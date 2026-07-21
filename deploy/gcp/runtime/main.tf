data "terraform_remote_state" "foundation" {
  backend = "gcs"
  config = {
    bucket = var.state_bucket
    prefix = var.foundation_state_prefix
  }
}

locals {
  foundation = data.terraform_remote_state.foundation.outputs.foundation
  prefix     = "attune-${local.foundation.environment}"
  common_labels = merge(
    {
      application = "attune"
      environment = local.foundation.environment
      managed_by  = "terraform"
    },
    var.labels,
  )
  audit_writer_labels      = merge(local.common_labels, { component = "audit-writer" })
  channel_broker_labels    = merge(local.common_labels, { component = "channel-broker" })
  dispatch_broker_labels   = merge(local.common_labels, { component = "dispatch-broker" })
  secret_broker_labels     = merge(local.common_labels, { component = "secret-broker" })
  oauth_exchange_labels    = merge(local.common_labels, { component = "oauth-exchange" })
  model_gateway_labels     = merge(local.common_labels, { component = "model-gateway" })
  worker_labels            = merge(local.common_labels, { component = "worker" })
  export_writer_labels     = merge(local.common_labels, { component = "export-writer" })
  dispatch_broker_audience = "https://${local.prefix}-dispatch-broker.attune.internal"
  secret_broker_audience   = "https://${local.prefix}-secret-broker.attune.internal"
  oauth_exchange_audience  = "https://${local.prefix}-oauth-exchange.attune.internal"
  worker_audience          = "https://${local.prefix}-worker.attune.internal"
  model_gateway_audience   = "https://${local.prefix}-model-gateway.attune.internal"
  export_writer_audience   = "https://${local.prefix}-export-writer.attune.internal"
  audit_callers = toset([
    local.foundation.workload_identities.control_plane,
    local.foundation.workload_identities.channel_broker,
    local.foundation.workload_identities.dispatch_broker,
    local.foundation.workload_identities.secret_broker,
    local.foundation.workload_identities.worker,
  ])
  channel_broker_audience = "https://${local.prefix}-channel-broker.attune.internal"
  # The worker's HTTP surface is one uniform /v1/tasks/dispatch route for
  # every task purpose, so the customer-facing "conversation execution"
  # p95 latency the SLO alert below cares about cannot be read off the
  # HTTP request metric -- it must filter the task_execution metric to
  # exactly whichever bounded conversation task kinds are activated.
  conversation_task_purposes = concat(
    var.enable_google_chat_conversation ? ["channel.google_chat.converse"] : [],
    var.enable_slack_conversation ? ["channel.slack.converse"] : [],
    var.enable_web_conversation ? ["channel.web.converse"] : [],
  )
}

resource "google_cloud_run_v2_service" "audit_writer" {
  project             = local.foundation.project_id
  name                = "${local.prefix}-audit-writer"
  location            = local.foundation.region
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  labels              = local.audit_writer_labels

  template {
    service_account                  = local.foundation.workload_identities.audit_writer
    timeout                          = "30s"
    max_instance_request_concurrency = 8

    scaling {
      min_instance_count = var.oauth_min_instance_count
      max_instance_count = 3
    }

    containers {
      name  = "audit-writer"
      image = var.audit_writer_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "ATTUNE_CLOUD_SQL_INSTANCE"
        value = local.foundation.database_instance
      }
      env {
        name  = "ATTUNE_DB_NAME"
        value = local.foundation.database_name
      }
      env {
        name = "ATTUNE_DB_USER"
        value = trimsuffix(
          local.foundation.workload_identities.audit_writer,
          ".gserviceaccount.com",
        )
      }

      startup_probe {
        initial_delay_seconds = 1
        timeout_seconds       = 2
        period_seconds        = 3
        failure_threshold     = 10
        http_get {
          path = "/healthz"
          port = 8080
        }
      }

      liveness_probe {
        timeout_seconds   = 2
        period_seconds    = 10
        failure_threshold = 3
        http_get {
          path = "/healthz"
          port = 8080
        }
      }
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = local.foundation.network_id
        subnetwork = local.foundation.subnetwork_id
        tags       = ["attune-audit-writer"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_cloud_run_v2_service_iam_member" "audit_invoker" {
  for_each = local.audit_callers
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.audit_writer.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value}"
}

resource "google_cloud_run_v2_service" "worker" {
  project             = local.foundation.project_id
  name                = "${local.prefix}-worker"
  location            = local.foundation.region
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  custom_audiences    = [local.worker_audience]
  labels              = local.worker_labels

  template {
    service_account                  = local.foundation.workload_identities.worker
    timeout                          = "30s"
    max_instance_request_concurrency = 4

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      name  = "worker"
      image = var.worker_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "ATTUNE_CLOUD_SQL_INSTANCE"
        value = local.foundation.database_instance
      }
      env {
        name  = "ATTUNE_DB_NAME"
        value = local.foundation.database_name
      }
      env {
        name = "ATTUNE_DB_USER"
        value = trimsuffix(
          local.foundation.workload_identities.worker,
          ".gserviceaccount.com",
        )
      }
      env {
        name  = "ATTUNE_AUDIT_WRITER_URL"
        value = google_cloud_run_v2_service.audit_writer.uri
      }
      env {
        name  = "ATTUNE_EXPECTED_AUDIENCE"
        value = local.worker_audience
      }
      env {
        name  = "ATTUNE_TASK_DISPATCH_SERVICE_ACCOUNT"
        value = local.foundation.workload_identities.task_dispatch
      }
      env {
        name  = "ATTUNE_ENABLE_GOOGLE_GMAIL_PROFILE"
        value = tostring(var.enable_google_gmail_profile)
      }
      env {
        name  = "ATTUNE_ENABLE_GOOGLE_WORKSPACE_VERIFICATION"
        value = tostring(var.enable_google_workspace_verification)
      }
      env {
        name  = "ATTUNE_ENABLE_GOOGLE_CHAT_CONVERSATION"
        value = tostring(var.enable_google_chat_conversation)
      }
      env {
        name  = "ATTUNE_ENABLE_SLACK_CONVERSATION"
        value = tostring(var.enable_slack_conversation)
      }
      env {
        name  = "ATTUNE_ENABLE_WEB_CONVERSATION"
        value = tostring(var.enable_web_conversation)
      }
      env {
        name  = "ATTUNE_HOSTED_TIMEZONE"
        value = var.hosted_timezone
      }
      env {
        name  = "ATTUNE_SECRET_BROKER_URL"
        value = google_cloud_run_v2_service.secret_broker.uri
      }
      env {
        name  = "ATTUNE_SECRET_BROKER_AUDIENCE"
        value = local.secret_broker_audience
      }
      dynamic "env" {
        for_each = var.enable_google_chat_conversation || var.enable_slack_conversation || var.enable_web_conversation ? [1] : []
        content {
          name  = "ATTUNE_MODEL_GATEWAY_URL"
          value = google_cloud_run_v2_service.model_gateway[0].uri
        }
      }
      dynamic "env" {
        for_each = var.enable_google_chat_conversation || var.enable_slack_conversation || var.enable_web_conversation ? [1] : []
        content {
          name  = "ATTUNE_MODEL_GATEWAY_AUDIENCE"
          value = local.model_gateway_audience
        }
      }
      # The web conversation route never touches the channel broker: it is
      # excluded from this gate on purpose. The hosted brief executor also
      # delivers through the channel broker, so it joins this condition too.
      dynamic "env" {
        for_each = var.enable_google_chat_conversation || var.enable_slack_conversation || var.enable_hosted_brief ? [1] : []
        content {
          name  = "ATTUNE_CHANNEL_BROKER_URL"
          value = google_cloud_run_v2_service.channel_broker[0].uri
        }
      }
      dynamic "env" {
        for_each = var.enable_google_chat_conversation || var.enable_slack_conversation || var.enable_hosted_brief ? [1] : []
        content {
          name  = "ATTUNE_CHANNEL_BROKER_AUDIENCE"
          value = local.channel_broker_audience
        }
      }
      # --- Hosted memory, draft-and-approve capability, briefs, per-tenant
      # model profiles, and usage metering (docs/hosted-memory.md,
      # docs/capability-gateway.md, docs/hosted-channels.md "Proactive brief
      # delivery", docs/hosted-model-profiles.md). Each is implemented and
      # tested behind its own default-off gate; wiring the flag here does not
      # by itself make any of them reachable by a tenant -- see each doc's
      # own "Deployment order"/"Activation gates" section for what evidence
      # must precede flipping it in a reviewed plan.
      env {
        name  = "ATTUNE_ENABLE_HOSTED_MEMORY"
        value = tostring(var.enable_hosted_memory)
      }
      env {
        name  = "ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY"
        value = tostring(var.enable_hosted_draft_capability)
      }
      env {
        name  = "ATTUNE_ENABLE_HOSTED_BRIEF"
        value = tostring(var.enable_hosted_brief)
      }
      env {
        name  = "ATTUNE_ENABLE_TENANT_MODEL_PROFILES"
        value = tostring(var.enable_tenant_model_profiles)
      }
      env {
        name  = "ATTUNE_ENABLE_MODEL_USAGE_METERING"
        value = tostring(var.enable_model_usage_metering)
      }
      # Both the draft-capability registry's signal capture and the hosted
      # brief executor derive a domain-separated reference hash from this
      # secret (worker_app.py's _intelligence_reference_hasher()).
      dynamic "env" {
        for_each = var.enable_hosted_draft_capability || var.enable_hosted_brief ? [1] : []
        content {
          name  = "ATTUNE_INTELLIGENCE_HMAC_SECRET"
          value = local.foundation.platform_secret_ids["intelligence-reference-hmac"]
        }
      }
      # Draft-capability admissions are enqueued through the dispatch broker
      # exactly like every other worker-originated job.
      dynamic "env" {
        for_each = var.enable_hosted_draft_capability ? [1] : []
        content {
          name  = "ATTUNE_DISPATCH_BROKER_URL"
          value = google_cloud_run_v2_service.dispatch_broker[0].uri
        }
      }
      dynamic "env" {
        for_each = var.enable_hosted_draft_capability ? [1] : []
        content {
          name  = "ATTUNE_DISPATCH_BROKER_AUDIENCE"
          value = local.dispatch_broker_audience
        }
      }

      startup_probe {
        initial_delay_seconds = 1
        timeout_seconds       = 2
        period_seconds        = 3
        failure_threshold     = 10
        http_get {
          path = "/healthz"
          port = 8080
        }
      }

      liveness_probe {
        timeout_seconds   = 2
        period_seconds    = 10
        failure_threshold = 3
        http_get {
          path = "/healthz"
          port = 8080
        }
      }
    }

    vpc_access {
      # The worker calls private Cloud Run services by their HTTPS run.app
      # origins. ALL_TRAFFIC preserves internal-ingress provenance; without
      # Cloud NAT it also fails closed for arbitrary internet egress.
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = local.foundation.network_id
        subnetwork = local.foundation.subnetwork_id
        tags       = ["attune-worker"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition = (
        !(var.enable_google_gmail_profile || var.enable_google_workspace_verification) ||
        length(var.alert_notification_channels) > 0
      )
      error_message = "Google provider-read activation requires at least one verified paging notification channel."
    }

    precondition {
      condition = (
        !(var.enable_google_gmail_profile || var.enable_google_workspace_verification) ||
        var.enable_dispatch_broker
      )
      error_message = "Google provider-read activation requires the fixed dispatch broker."
    }

    precondition {
      condition = !var.enable_google_chat_conversation || (
        var.enable_dispatch_broker &&
        var.enable_channel_broker &&
        var.enable_model_gateway &&
        length(var.alert_notification_channels) > 0
      )
      error_message = "Google Chat conversation activation requires dispatch, channel, model, and paging boundaries."
    }

    precondition {
      condition = !var.enable_web_conversation || (
        var.enable_dispatch_broker &&
        var.enable_model_gateway &&
        length(var.alert_notification_channels) > 0
      )
      error_message = "Web conversation activation requires the dispatch and model boundaries and paging; it never touches the channel broker."
    }

    precondition {
      condition     = !var.enable_hosted_draft_capability || var.enable_dispatch_broker
      error_message = "The draft-and-approve capability gateway enqueues admissions through the dispatch broker and requires it deployed."
    }

    precondition {
      condition     = !var.enable_hosted_brief || var.enable_channel_broker
      error_message = "Hosted brief delivery proposes rendered briefs through the channel broker and requires it deployed."
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "worker_invoker" {
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.worker.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.task_dispatch}"
}

resource "google_cloud_run_v2_service" "model_gateway" {
  count               = var.enable_model_gateway ? 1 : 0
  project             = local.foundation.project_id
  name                = "${local.prefix}-model-gateway"
  location            = local.foundation.region
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  custom_audiences    = [local.model_gateway_audience]
  labels              = local.model_gateway_labels

  template {
    service_account                  = local.foundation.workload_identities.model_gateway
    timeout                          = "30s"
    max_instance_request_concurrency = 4

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      name  = "model-gateway"
      image = var.model_gateway_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "ATTUNE_LLM_BASE_URL"
        value = var.llm_base_url
      }
      env {
        name  = "ATTUNE_MODEL_CLASSIFY"
        value = var.model_classify
      }
      env {
        name  = "ATTUNE_MODEL_CONVERSE"
        value = var.model_converse
      }
      env {
        # model_gateway_app.py reads this unconditionally as part of the
        # fixed standard_models map (classify/converse/embed) -- previously
        # unwired here, which would have crashed the gateway on first boot
        # regardless of any feature gate.
        name  = "ATTUNE_MODEL_EMBED"
        value = var.model_embed
      }
      env {
        name  = "ATTUNE_EXPECTED_AUDIENCE"
        value = local.model_gateway_audience
      }
      env {
        name  = "ATTUNE_WORKER_SERVICE_ACCOUNT"
        value = local.foundation.workload_identities.worker
      }
      env {
        name = "ATTUNE_LLM_API_KEY"
        value_source {
          secret_key_ref {
            secret  = local.foundation.platform_secret_ids["llm-api-key"]
            version = "latest"
          }
        }
      }
      # Per-tenant model profiles (docs/hosted-model-profiles.md): gate off
      # (the default) is the pinned byte-identical routing path; gate on
      # joins these operator-fixed premium routes with the standard map
      # into one profile mapping, never a tenant- or worker-supplied
      # endpoint.
      env {
        name  = "ATTUNE_ENABLE_TENANT_MODEL_PROFILES"
        value = tostring(var.enable_tenant_model_profiles)
      }
      dynamic "env" {
        for_each = var.enable_tenant_model_profiles ? [1] : []
        content {
          name  = "ATTUNE_MODEL_PREMIUM_CLASSIFY"
          value = var.model_premium_classify
        }
      }
      dynamic "env" {
        for_each = var.enable_tenant_model_profiles ? [1] : []
        content {
          name  = "ATTUNE_MODEL_PREMIUM_CONVERSE"
          value = var.model_premium_converse
        }
      }
      dynamic "env" {
        for_each = var.enable_tenant_model_profiles ? [1] : []
        content {
          name  = "ATTUNE_MODEL_PREMIUM_EMBED"
          value = var.model_premium_embed
        }
      }

      startup_probe {
        initial_delay_seconds = 1
        timeout_seconds       = 2
        period_seconds        = 3
        failure_threshold     = 10
        http_get {
          path = "/healthz"
          port = 8080
        }
      }

      liveness_probe {
        timeout_seconds   = 2
        period_seconds    = 10
        failure_threshold = 3
        http_get {
          path = "/healthz"
          port = 8080
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition = !var.enable_tenant_model_profiles || (
        var.model_premium_classify != "" &&
        var.model_premium_converse != "" &&
        var.model_premium_embed != ""
      )
      error_message = "Tenant model profile activation requires all three fixed premium model routes."
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "model_gateway_invoker" {
  count    = var.enable_model_gateway ? 1 : 0
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.model_gateway[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.worker}"
}

resource "google_cloud_run_v2_service" "dispatch_broker" {
  count               = var.enable_dispatch_broker ? 1 : 0
  project             = local.foundation.project_id
  name                = "${local.prefix}-dispatch-broker"
  location            = local.foundation.region
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  custom_audiences    = [local.dispatch_broker_audience]
  labels              = local.dispatch_broker_labels

  template {
    service_account                  = local.foundation.workload_identities.dispatch_broker
    timeout                          = "30s"
    max_instance_request_concurrency = 4

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      name  = "dispatch-broker"
      image = var.dispatch_broker_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "ATTUNE_CLOUD_SQL_INSTANCE"
        value = local.foundation.database_instance
      }
      env {
        name  = "ATTUNE_DB_NAME"
        value = local.foundation.database_name
      }
      env {
        name = "ATTUNE_DB_USER"
        value = trimsuffix(
          local.foundation.workload_identities.dispatch_broker,
          ".gserviceaccount.com",
        )
      }
      env {
        name  = "ATTUNE_AUDIT_WRITER_URL"
        value = google_cloud_run_v2_service.audit_writer.uri
      }
      env {
        name  = "ATTUNE_EXPECTED_AUDIENCE"
        value = local.dispatch_broker_audience
      }
      env {
        name  = "ATTUNE_CONTROL_PLANE_SERVICE_ACCOUNT"
        value = local.foundation.workload_identities.control_plane
      }
      env {
        name  = "ATTUNE_INGRESS_SERVICE_ACCOUNT"
        value = local.foundation.workload_identities.ingress
      }
      dynamic "env" {
        for_each = var.enable_slack_conversation ? [1] : []
        content {
          name  = "ATTUNE_SLACK_INGRESS_SERVICE_ACCOUNT"
          value = local.foundation.workload_identities.slack_ingress
        }
      }
      env {
        name  = "ATTUNE_WORKER_SERVICE_ACCOUNT"
        value = local.foundation.workload_identities.worker
      }
      env {
        name  = "ATTUNE_TASK_DISPATCH_SERVICE_ACCOUNT"
        value = local.foundation.workload_identities.task_dispatch
      }
      env {
        name = "ATTUNE_DISPATCH_ROUTES"
        value = jsonencode(concat(
          [
            {
              purpose    = "platform.smoke"
              queue      = local.foundation.jobs_queue
              target_url = "${google_cloud_run_v2_service.worker.uri}/v1/tasks/dispatch"
              audience   = local.worker_audience
            }
          ],
          var.enable_google_gmail_profile ? [
            {
              purpose    = "google.gmail.profile.read"
              queue      = local.foundation.jobs_queue
              target_url = "${google_cloud_run_v2_service.worker.uri}/v1/tasks/dispatch"
              audience   = local.worker_audience
            }
          ] : [],
          var.enable_google_workspace_verification ? [
            {
              purpose    = "google.workspace.connection.verify"
              queue      = local.foundation.jobs_queue
              target_url = "${google_cloud_run_v2_service.worker.uri}/v1/tasks/dispatch"
              audience   = local.worker_audience
            }
          ] : [],
          var.enable_google_chat_conversation ? [
            {
              purpose    = "channel.google_chat.converse"
              queue      = local.foundation.jobs_queue
              target_url = "${google_cloud_run_v2_service.worker.uri}/v1/tasks/dispatch"
              audience   = local.worker_audience
            }
          ] : [],
          var.enable_slack_conversation ? [
            {
              purpose    = "channel.slack.converse"
              queue      = local.foundation.jobs_queue
              target_url = "${google_cloud_run_v2_service.worker.uri}/v1/tasks/dispatch"
              audience   = local.worker_audience
            }
          ] : [],
          var.enable_web_conversation ? [
            {
              purpose    = "channel.web.converse"
              queue      = local.foundation.jobs_queue
              target_url = "${google_cloud_run_v2_service.worker.uri}/v1/tasks/dispatch"
              audience   = local.worker_audience
            }
          ] : [],
          var.enable_export_writer ? [
            {
              purpose    = "customer.export.generate"
              queue      = local.foundation.jobs_queue
              target_url = "${google_cloud_run_v2_service.export_writer[0].uri}/v1/tasks/customer-export"
              audience   = local.export_writer_audience
            }
          ] : [],
        ))
      }

      startup_probe {
        initial_delay_seconds = 1
        timeout_seconds       = 2
        period_seconds        = 3
        failure_threshold     = 10
        http_get {
          path = "/healthz"
          port = 8080
        }
      }

      liveness_probe {
        timeout_seconds   = 2
        period_seconds    = 10
        failure_threshold = 3
        http_get {
          path = "/healthz"
          port = 8080
        }
      }
    }

    vpc_access {
      # The broker must reach the internal audit writer through this VPC.
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = local.foundation.network_id
        subnetwork = local.foundation.subnetwork_id
        tags       = ["attune-dispatch-broker"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_cloud_run_v2_service_iam_member" "dispatch_broker_invoker" {
  # The Slack ingress runs on its own identity (distinct broker-caller
  # attribution) and therefore needs its own dispatch invoker grant when
  # Slack conversations are enabled.
  for_each = var.enable_dispatch_broker ? toset(concat(
    [
      local.foundation.workload_identities.control_plane,
      local.foundation.workload_identities.ingress,
      local.foundation.workload_identities.worker,
    ],
    var.enable_slack_conversation ? [
      local.foundation.workload_identities.slack_ingress,
    ] : [],
  )) : toset([])
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.dispatch_broker[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value}"
}

resource "google_cloud_run_v2_service" "export_writer" {
  count               = var.enable_export_writer ? 1 : 0
  project             = local.foundation.project_id
  name                = "${local.prefix}-export-writer"
  location            = local.foundation.region
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  custom_audiences    = [local.export_writer_audience]
  labels              = local.export_writer_labels

  template {
    service_account                  = local.foundation.workload_identities.export
    timeout                          = "300s"
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      name  = "export-writer"
      image = var.export_writer_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "1Gi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "ATTUNE_CLOUD_SQL_INSTANCE"
        value = local.foundation.database_instance
      }
      env {
        name  = "ATTUNE_DB_NAME"
        value = local.foundation.database_name
      }
      env {
        name = "ATTUNE_DB_USER"
        value = trimsuffix(
          local.foundation.workload_identities.export,
          ".gserviceaccount.com",
        )
      }
      env {
        name  = "ATTUNE_EXPORT_BUCKET"
        value = local.foundation.customer_export_bucket
      }
      env {
        name  = "ATTUNE_EXPORT_KMS_KEY"
        value = local.foundation.customer_export_kms_key
      }
      env {
        name  = "ATTUNE_EXPECTED_AUDIENCE"
        value = local.export_writer_audience
      }
      env {
        name  = "ATTUNE_TASK_DISPATCH_SERVICE_ACCOUNT"
        value = local.foundation.workload_identities.task_dispatch
      }

      startup_probe {
        initial_delay_seconds = 1
        timeout_seconds       = 2
        period_seconds        = 3
        failure_threshold     = 10
        http_get {
          path = "/healthz"
          port = 8080
        }
      }

      liveness_probe {
        timeout_seconds   = 2
        period_seconds    = 10
        failure_threshold = 3
        http_get {
          path = "/healthz"
          port = 8080
        }
      }
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = local.foundation.network_id
        subnetwork = local.foundation.subnetwork_id
        tags       = ["attune-export-writer"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_cloud_run_v2_service_iam_member" "export_writer_invoker" {
  count    = var.enable_export_writer ? 1 : 0
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.export_writer[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.task_dispatch}"
}

resource "google_cloud_run_v2_service" "channel_broker" {
  count               = var.enable_channel_broker ? 1 : 0
  project             = local.foundation.project_id
  name                = "${local.prefix}-channel-broker"
  location            = local.foundation.region
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  custom_audiences    = [local.channel_broker_audience]
  labels              = local.channel_broker_labels

  template {
    service_account                  = local.foundation.workload_identities.channel_broker
    timeout                          = "30s"
    max_instance_request_concurrency = 4

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      name  = "channel-broker"
      image = var.channel_broker_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "ATTUNE_CLOUD_SQL_INSTANCE"
        value = local.foundation.database_instance
      }
      env {
        name  = "ATTUNE_DB_NAME"
        value = local.foundation.database_name
      }
      env {
        name = "ATTUNE_DB_USER"
        value = trimsuffix(
          local.foundation.workload_identities.channel_broker,
          ".gserviceaccount.com",
        )
      }
      env {
        name  = "ATTUNE_AUDIT_WRITER_URL"
        value = google_cloud_run_v2_service.audit_writer.uri
      }
      env {
        name  = "ATTUNE_EXPECTED_AUDIENCE"
        value = local.channel_broker_audience
      }
      env {
        name  = "ATTUNE_INGRESS_SERVICE_ACCOUNT"
        value = local.foundation.workload_identities.ingress
      }
      env {
        name  = "ATTUNE_CONTROL_PLANE_SERVICE_ACCOUNT"
        value = local.foundation.workload_identities.control_plane
      }
      env {
        name  = "ATTUNE_WORKER_SERVICE_ACCOUNT"
        value = local.foundation.workload_identities.worker
      }
      env {
        name  = "ATTUNE_CHANNEL_HMAC_SECRET"
        value = local.foundation.platform_secret_ids["channel-reference-hmac"]
      }
      env {
        name  = "ATTUNE_CONNECTOR_KMS_KEY"
        value = local.foundation.connector_kms_key
      }
      env {
        name  = "ATTUNE_SLACK_CHANNEL_ENABLED"
        value = tostring(var.slack_channel_enabled)
      }
      dynamic "env" {
        for_each = var.slack_channel_enabled ? [1] : []
        content {
          name  = "ATTUNE_SLACK_CLIENT_ID"
          value = var.slack_client_id
        }
      }
      dynamic "env" {
        for_each = var.slack_channel_enabled ? [1] : []
        content {
          name  = "ATTUNE_SLACK_CLIENT_SECRET"
          value = local.foundation.platform_secret_ids["slack-client"]
        }
      }
      dynamic "env" {
        for_each = var.slack_channel_enabled ? [1] : []
        content {
          name  = "ATTUNE_SLACK_APP_ID"
          value = var.slack_app_id
        }
      }
      dynamic "env" {
        for_each = var.slack_channel_enabled ? [1] : []
        content {
          name  = "ATTUNE_SLACK_REDIRECT_URI"
          value = var.slack_redirect_uri
        }
      }
      dynamic "env" {
        for_each = var.slack_channel_enabled ? [1] : []
        content {
          name  = "ATTUNE_SLACK_INGRESS_SERVICE_ACCOUNT"
          value = local.foundation.workload_identities.slack_ingress
        }
      }

      startup_probe {
        initial_delay_seconds = 1
        timeout_seconds       = 2
        period_seconds        = 3
        failure_threshold     = 10
        http_get {
          path = "/healthz"
          port = 8080
        }
      }

      liveness_probe {
        timeout_seconds   = 2
        period_seconds    = 10
        failure_threshold = 3
        http_get {
          path = "/healthz"
          port = 8080
        }
      }
    }

    vpc_access {
      # The broker needs VPC provenance for the internal audit writer AND
      # NAT'd internet egress for the Slack provider API (ordinary internet,
      # unlike Google Chat's Private-Google-Access path). It sits on a
      # dedicated broker-egress subnetwork whose Cloud NAT covers only this
      # subnet, so every other workload keeps the no-NAT fail-closed posture.
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = local.foundation.network_id
        subnetwork = local.foundation.broker_egress_subnetwork_id
        tags       = ["attune-channel-broker"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition = !var.slack_channel_enabled || (
        var.slack_client_id != "" &&
        var.slack_app_id != "" &&
        var.slack_redirect_uri != ""
      )
      error_message = "Slack channel activation requires the platform Slack app client ID, app ID, and exact redirect URI."
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "channel_broker_invoker" {
  count    = var.enable_channel_broker ? 1 : 0
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.channel_broker[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.ingress}"
}

resource "google_cloud_run_v2_service_iam_member" "channel_broker_slack_ingress_invoker" {
  count    = var.enable_channel_broker ? 1 : 0
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.channel_broker[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.slack_ingress}"
}

resource "google_cloud_run_v2_service_iam_member" "channel_broker_control_plane_invoker" {
  count    = var.enable_channel_broker ? 1 : 0
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.channel_broker[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.control_plane}"
}

resource "google_cloud_run_v2_service_iam_member" "channel_broker_worker_invoker" {
  count    = var.enable_channel_broker && (var.enable_google_chat_conversation || var.enable_slack_conversation) ? 1 : 0
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.channel_broker[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.worker}"
}

resource "google_cloud_run_v2_service" "secret_broker" {
  project             = local.foundation.project_id
  name                = "${local.prefix}-secret-broker"
  location            = local.foundation.region
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  custom_audiences    = [local.secret_broker_audience]
  labels              = local.secret_broker_labels

  template {
    service_account                  = local.foundation.workload_identities.secret_broker
    timeout                          = "30s"
    max_instance_request_concurrency = 4

    scaling {
      min_instance_count = var.oauth_min_instance_count
      max_instance_count = 3
    }

    containers {
      name  = "secret-broker"
      image = var.secret_broker_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "ATTUNE_CLOUD_SQL_INSTANCE"
        value = local.foundation.database_instance
      }
      env {
        name  = "ATTUNE_DB_NAME"
        value = local.foundation.database_name
      }
      env {
        name = "ATTUNE_DB_USER"
        value = trimsuffix(
          local.foundation.workload_identities.secret_broker,
          ".gserviceaccount.com",
        )
      }
      env {
        name  = "ATTUNE_CONNECTOR_KMS_KEY"
        value = local.foundation.connector_kms_key
      }
      env {
        name  = "ATTUNE_AUDIT_WRITER_URL"
        value = google_cloud_run_v2_service.audit_writer.uri
      }
      env {
        name  = "ATTUNE_EXPECTED_AUDIENCE"
        value = local.secret_broker_audience
      }
      env {
        name  = "ATTUNE_CONTROL_PLANE_SERVICE_ACCOUNT"
        value = local.foundation.workload_identities.control_plane
      }
      env {
        name  = "ATTUNE_WORKER_SERVICE_ACCOUNT"
        value = local.foundation.workload_identities.worker
      }
      env {
        name  = "ATTUNE_OAUTH_EXCHANGE_SERVICE_ACCOUNT"
        value = local.foundation.workload_identities.oauth_exchange
      }
      env {
        name  = "ATTUNE_GOOGLE_OAUTH_CLIENT_SECRET"
        value = local.foundation.platform_secret_ids["google-oauth-client"]
      }

      startup_probe {
        initial_delay_seconds = 1
        timeout_seconds       = 2
        period_seconds        = 3
        failure_threshold     = 10
        http_get {
          path = "/healthz"
          port = 8080
        }
      }

      liveness_probe {
        timeout_seconds   = 2
        period_seconds    = 10
        failure_threshold = 3
        http_get {
          path = "/healthz"
          port = 8080
        }
      }
    }

    vpc_access {
      # The secret broker must reach the internal audit writer through this VPC.
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = local.foundation.network_id
        subnetwork = local.foundation.subnetwork_id
        tags       = ["attune-secret-broker"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_cloud_run_v2_service_iam_member" "secret_broker_invoker" {
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.secret_broker.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.control_plane}"
}

resource "google_cloud_run_v2_service_iam_member" "secret_broker_worker_invoker" {
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.secret_broker.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.worker}"
}

resource "google_cloud_run_v2_service_iam_member" "secret_broker_oauth_invoker" {
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.secret_broker.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.oauth_exchange}"
}

resource "google_cloud_run_v2_service" "oauth_exchange" {
  project             = local.foundation.project_id
  name                = "${local.prefix}-oauth-exchange"
  location            = local.foundation.region
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  custom_audiences    = [local.oauth_exchange_audience]
  labels              = local.oauth_exchange_labels

  template {
    service_account                  = local.foundation.workload_identities.oauth_exchange
    timeout                          = "30s"
    max_instance_request_concurrency = 4

    scaling {
      min_instance_count = var.oauth_min_instance_count
      max_instance_count = 3
    }

    containers {
      name  = "oauth-exchange"
      image = var.oauth_exchange_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "ATTUNE_CLOUD_SQL_INSTANCE"
        value = local.foundation.database_instance
      }
      env {
        name  = "ATTUNE_DB_NAME"
        value = local.foundation.database_name
      }
      env {
        name = "ATTUNE_DB_USER"
        value = trimsuffix(
          local.foundation.workload_identities.oauth_exchange,
          ".gserviceaccount.com",
        )
      }
      env {
        name  = "ATTUNE_EXPECTED_AUDIENCE"
        value = local.oauth_exchange_audience
      }
      env {
        name  = "ATTUNE_OAUTH_CALLBACK_SERVICE_ACCOUNT"
        value = local.foundation.workload_identities.oauth_callback
      }
      env {
        name  = "ATTUNE_SECRET_BROKER_URL"
        value = google_cloud_run_v2_service.secret_broker.uri
      }
      env {
        name  = "ATTUNE_SECRET_BROKER_AUDIENCE"
        value = local.secret_broker_audience
      }

      startup_probe {
        initial_delay_seconds = 1
        timeout_seconds       = 2
        period_seconds        = 3
        failure_threshold     = 10
        http_get {
          path = "/healthz"
          port = 8080
        }
      }

      liveness_probe {
        timeout_seconds   = 2
        period_seconds    = 10
        failure_threshold = 3
        http_get {
          path = "/healthz"
          port = 8080
        }
      }
    }

    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = local.foundation.network_id
        subnetwork = local.foundation.subnetwork_id
        tags       = ["attune-oauth-exchange"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_cloud_run_v2_service_iam_member" "oauth_exchange_invoker" {
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.oauth_exchange.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.oauth_callback}"
}

resource "google_logging_metric" "secret_broker_use_anomaly" {
  project = local.foundation.project_id
  name    = "${local.prefix}-secret-broker-use-anomaly"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.secret_broker.name}\"",
    "severity>=WARNING",
    "textPayload:\"attune_secret_broker_use_anomaly\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Attune secret-broker use anomalies"
  }
}

resource "google_monitoring_alert_policy" "secret_broker_use_anomaly" {
  project      = local.foundation.project_id
  display_name = "${local.prefix} secret-broker use anomalies"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = <<-EOT
      The private secret broker returned more than five denied/rate-limited,
      provider-failed, or unavailable credential-use results within five
      minutes. Investigate workload identity, intent volume, provider health,
      and audit availability. The signal contains no tenant or provider data.
    EOT
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "More than five use anomalies in five minutes"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.secret_broker_use_anomaly.name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = var.alert_notification_channels
  user_labels           = local.common_labels
}

resource "google_logging_metric" "export_writer_failure" {
  count   = var.enable_export_writer ? 1 : 0
  project = local.foundation.project_id
  name    = "${local.prefix}-export-writer-failure"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.export_writer[0].name}\"",
    "severity>=WARNING",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Attune customer-export writer failures"
  }
}

resource "google_monitoring_alert_policy" "export_writer_failure" {
  count        = var.enable_export_writer ? 1 : 0
  project      = local.foundation.project_id
  display_name = "${local.prefix} customer-export writer failure"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = "Customer-export generation, exact cleanup, or task finalization failed. Inspect the execution without granting object read/list or KMS decrypt authority."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "At least one export writer warning"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.export_writer_failure[0].name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = var.alert_notification_channels
  user_labels           = local.common_labels
}

# --- SLO-grade observability (Phase 6 "hosted operations", hosted review
# gap #8: only seven job-failure alert policies existed; no latency/
# error-rate visibility, no dashboards, no per-service health signal).
#
# Each service's Flask app emits one content-free "http_request" JSON line
# per request (attune.hosted.service_metrics.instrument_service_metrics);
# the worker additionally emits one "task_execution" line per dispatched
# task (worker_dispatch.py). Both carry only fixed-vocabulary fields --
# service/route/method/status_class/status/duration_ms, or
# task/outcome/duration_ms -- never tenant, principal, query string, or
# any other identifier. See docs/decisions.md's 2026-07-19 SLO-monitoring
# entry for the field contract.
#
# These log-based metrics and alert policies are unconditional
# infrastructure, exactly like the seven pre-existing policies above: each
# is tied only to whether its underlying Cloud Run service itself is
# deployed (the same enable_* flag that gates the service), never to a
# separate "enable monitoring" toggle. Monitoring is not a customer-facing
# feature needing its own security-review gate; the existing
# secret_broker_use_anomaly and export_writer_failure policies above
# already establish that norm for this module. Metric emission in the
# application is unconditional too, for the same reason: it is
# content-free operational logging, strictly less sensitive than the
# anomaly markers and audit events these services already write.

resource "google_logging_metric" "worker_http_request_count" {
  project = local.foundation.project_id
  name    = "${local.prefix}-worker-http-request-count"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.worker.name}\"",
    "jsonPayload.metric=\"http_request\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Attune worker HTTP request count"

    labels {
      key         = "route"
      value_type  = "STRING"
      description = "Matched Flask URL rule template; never a raw path or identifier."
    }
    labels {
      key         = "method"
      value_type  = "STRING"
      description = "HTTP method."
    }
    labels {
      key         = "status_class"
      value_type  = "STRING"
      description = "Response status class (2xx..5xx)."
    }
  }

  label_extractors = {
    "route"        = "EXTRACT(jsonPayload.route)"
    "method"       = "EXTRACT(jsonPayload.method)"
    "status_class" = "EXTRACT(jsonPayload.status_class)"
  }
}

resource "google_logging_metric" "worker_http_request_latency" {
  project = local.foundation.project_id
  name    = "${local.prefix}-worker-http-request-latency"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.worker.name}\"",
    "jsonPayload.metric=\"http_request\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "ms"
    display_name = "Attune worker HTTP request latency"

    labels {
      key         = "route"
      value_type  = "STRING"
      description = "Matched Flask URL rule template; never a raw path or identifier."
    }
    labels {
      key         = "status_class"
      value_type  = "STRING"
      description = "Response status class (2xx..5xx)."
    }
  }

  value_extractor = "EXTRACT(jsonPayload.duration_ms)"
  label_extractors = {
    "route"        = "EXTRACT(jsonPayload.route)"
    "status_class" = "EXTRACT(jsonPayload.status_class)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 24
      growth_factor      = 2.0
      scale              = 5
    }
  }
}

resource "google_monitoring_alert_policy" "worker_5xx_error_rate" {
  project      = local.foundation.project_id
  display_name = "${local.prefix} worker 5xx error rate"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = <<-EOT
      The worker returned more than ${var.slo_5xx_error_threshold} 5xx
      responses within ${var.slo_alert_window_seconds} seconds. Inspect
      recent deploys, dependency health (audit writer, secret broker,
      model gateway), and reconciliation backlog. The signal contains no
      tenant or provider data -- only the fixed route, method, and status
      class.
    EOT
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "Worker 5xx responses over threshold"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"logging.googleapis.com/user/${google_logging_metric.worker_http_request_count.name}\"",
        "resource.type=\"cloud_run_revision\"",
        "metric.labels.status_class=\"5xx\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = var.slo_5xx_error_threshold
      duration        = "0s"

      aggregations {
        alignment_period     = "${var.slo_alert_window_seconds}s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = var.alert_notification_channels
  user_labels            = local.common_labels
}

resource "google_logging_metric" "worker_task_execution_count" {
  project = local.foundation.project_id
  name    = "${local.prefix}-worker-task-execution-count"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.worker.name}\"",
    "jsonPayload.metric=\"task_execution\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Attune worker task-execution count"

    labels {
      key         = "task"
      value_type  = "STRING"
      description = "Fixed registered task purpose (route.purpose)."
    }
    labels {
      key         = "outcome"
      value_type  = "STRING"
      description = "succeeded, duplicate, reconciled, or failed."
    }
  }

  label_extractors = {
    "task"    = "EXTRACT(jsonPayload.task)"
    "outcome" = "EXTRACT(jsonPayload.outcome)"
  }
}

resource "google_logging_metric" "worker_task_execution_latency" {
  project = local.foundation.project_id
  name    = "${local.prefix}-worker-task-execution-latency"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.worker.name}\"",
    "jsonPayload.metric=\"task_execution\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "ms"
    display_name = "Attune worker task-execution latency"

    labels {
      key         = "task"
      value_type  = "STRING"
      description = "Fixed registered task purpose (route.purpose)."
    }
  }

  value_extractor = "EXTRACT(jsonPayload.duration_ms)"
  label_extractors = {
    "task" = "EXTRACT(jsonPayload.task)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 24
      growth_factor      = 2.0
      scale              = 5
    }
  }
}

resource "google_monitoring_alert_policy" "worker_conversation_p95_latency" {
  count        = length(local.conversation_task_purposes) > 0 ? 1 : 0
  project      = local.foundation.project_id
  display_name = "${local.prefix} worker conversation-execution p95 latency"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = <<-EOT
      p95 execution latency for the bounded conversation task kinds
      (Google Chat, Slack, and/or web conversation execution, whichever
      are activated) exceeded ${var.slo_worker_conversation_p95_latency_ms}ms
      over ${var.slo_alert_window_seconds} seconds. Inspect the model
      gateway and its upstream provider before raising the threshold.
    EOT
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "Conversation task p95 latency over threshold"
    condition_threshold {
      filter = join(" AND ", concat(
        [
          "metric.type=\"logging.googleapis.com/user/${google_logging_metric.worker_task_execution_latency.name}\"",
          "resource.type=\"cloud_run_revision\"",
        ],
        [
          "(${join(" OR ", [
            for purpose in local.conversation_task_purposes :
            "metric.labels.task=\"${purpose}\""
          ])})",
        ],
      ))
      comparison      = "COMPARISON_GT"
      threshold_value = var.slo_worker_conversation_p95_latency_ms
      duration        = "0s"

      aggregations {
        alignment_period   = "${var.slo_alert_window_seconds}s"
        per_series_aligner = "ALIGN_PERCENTILE_95"
      }
    }
  }

  notification_channels = var.alert_notification_channels
  user_labels            = local.common_labels
}

resource "google_logging_metric" "model_gateway_http_request_count" {
  count   = var.enable_model_gateway ? 1 : 0
  project = local.foundation.project_id
  name    = "${local.prefix}-model-gateway-http-request-count"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.model_gateway[0].name}\"",
    "jsonPayload.metric=\"http_request\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Attune model-gateway HTTP request count"

    labels {
      key         = "route"
      value_type  = "STRING"
      description = "Matched Flask URL rule template; never a raw path or identifier."
    }
    labels {
      key         = "method"
      value_type  = "STRING"
      description = "HTTP method."
    }
    labels {
      key         = "status_class"
      value_type  = "STRING"
      description = "Response status class (2xx..5xx)."
    }
  }

  label_extractors = {
    "route"        = "EXTRACT(jsonPayload.route)"
    "method"       = "EXTRACT(jsonPayload.method)"
    "status_class" = "EXTRACT(jsonPayload.status_class)"
  }
}

resource "google_logging_metric" "model_gateway_http_request_latency" {
  count   = var.enable_model_gateway ? 1 : 0
  project = local.foundation.project_id
  name    = "${local.prefix}-model-gateway-http-request-latency"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.model_gateway[0].name}\"",
    "jsonPayload.metric=\"http_request\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "ms"
    display_name = "Attune model-gateway HTTP request latency"

    labels {
      key         = "route"
      value_type  = "STRING"
      description = "Matched Flask URL rule template; never a raw path or identifier."
    }
    labels {
      key         = "status_class"
      value_type  = "STRING"
      description = "Response status class (2xx..5xx)."
    }
  }

  value_extractor = "EXTRACT(jsonPayload.duration_ms)"
  label_extractors = {
    "route"        = "EXTRACT(jsonPayload.route)"
    "status_class" = "EXTRACT(jsonPayload.status_class)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 24
      growth_factor      = 2.0
      scale              = 5
    }
  }
}

resource "google_monitoring_alert_policy" "model_gateway_5xx_error_rate" {
  count        = var.enable_model_gateway ? 1 : 0
  project      = local.foundation.project_id
  display_name = "${local.prefix} model-gateway 5xx error rate"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = "The model gateway returned more than ${var.slo_5xx_error_threshold} 5xx responses within ${var.slo_alert_window_seconds} seconds. Inspect the upstream LLM base URL's health and rate limits before assuming a gateway bug; the signal contains no prompt, response, or provider content."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "Model-gateway 5xx responses over threshold"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"logging.googleapis.com/user/${google_logging_metric.model_gateway_http_request_count[0].name}\"",
        "resource.type=\"cloud_run_revision\"",
        "metric.labels.status_class=\"5xx\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = var.slo_5xx_error_threshold
      duration        = "0s"

      aggregations {
        alignment_period     = "${var.slo_alert_window_seconds}s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = var.alert_notification_channels
  user_labels            = local.common_labels
}

resource "google_logging_metric" "dispatch_broker_http_request_count" {
  count   = var.enable_dispatch_broker ? 1 : 0
  project = local.foundation.project_id
  name    = "${local.prefix}-dispatch-broker-http-request-count"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.dispatch_broker[0].name}\"",
    "jsonPayload.metric=\"http_request\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Attune dispatch-broker HTTP request count"

    labels {
      key         = "route"
      value_type  = "STRING"
      description = "Matched Flask URL rule template; never a raw path or identifier."
    }
    labels {
      key         = "method"
      value_type  = "STRING"
      description = "HTTP method."
    }
    labels {
      key         = "status_class"
      value_type  = "STRING"
      description = "Response status class (2xx..5xx)."
    }
  }

  label_extractors = {
    "route"        = "EXTRACT(jsonPayload.route)"
    "method"       = "EXTRACT(jsonPayload.method)"
    "status_class" = "EXTRACT(jsonPayload.status_class)"
  }
}

resource "google_logging_metric" "dispatch_broker_http_request_latency" {
  count   = var.enable_dispatch_broker ? 1 : 0
  project = local.foundation.project_id
  name    = "${local.prefix}-dispatch-broker-http-request-latency"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.dispatch_broker[0].name}\"",
    "jsonPayload.metric=\"http_request\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "ms"
    display_name = "Attune dispatch-broker HTTP request latency"

    labels {
      key         = "route"
      value_type  = "STRING"
      description = "Matched Flask URL rule template; never a raw path or identifier."
    }
    labels {
      key         = "status_class"
      value_type  = "STRING"
      description = "Response status class (2xx..5xx)."
    }
  }

  value_extractor = "EXTRACT(jsonPayload.duration_ms)"
  label_extractors = {
    "route"        = "EXTRACT(jsonPayload.route)"
    "status_class" = "EXTRACT(jsonPayload.status_class)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 24
      growth_factor      = 2.0
      scale              = 5
    }
  }
}

resource "google_monitoring_alert_policy" "dispatch_broker_5xx_error_rate" {
  count        = var.enable_dispatch_broker ? 1 : 0
  project      = local.foundation.project_id
  display_name = "${local.prefix} dispatch-broker 5xx error rate"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = "The dispatch broker returned more than ${var.slo_5xx_error_threshold} 5xx responses within ${var.slo_alert_window_seconds} seconds. Inspect Cloud Tasks enqueue health and the fixed dispatch-route table; the signal contains no tenant or intent content."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "Dispatch-broker 5xx responses over threshold"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"logging.googleapis.com/user/${google_logging_metric.dispatch_broker_http_request_count[0].name}\"",
        "resource.type=\"cloud_run_revision\"",
        "metric.labels.status_class=\"5xx\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = var.slo_5xx_error_threshold
      duration        = "0s"

      aggregations {
        alignment_period     = "${var.slo_alert_window_seconds}s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = var.alert_notification_channels
  user_labels            = local.common_labels
}

resource "google_logging_metric" "secret_broker_http_request_count" {
  project = local.foundation.project_id
  name    = "${local.prefix}-secret-broker-http-request-count"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.secret_broker.name}\"",
    "jsonPayload.metric=\"http_request\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Attune secret-broker HTTP request count"

    labels {
      key         = "route"
      value_type  = "STRING"
      description = "Matched Flask URL rule template; never a raw path or identifier."
    }
    labels {
      key         = "method"
      value_type  = "STRING"
      description = "HTTP method."
    }
    labels {
      key         = "status_class"
      value_type  = "STRING"
      description = "Response status class (2xx..5xx)."
    }
  }

  label_extractors = {
    "route"        = "EXTRACT(jsonPayload.route)"
    "method"       = "EXTRACT(jsonPayload.method)"
    "status_class" = "EXTRACT(jsonPayload.status_class)"
  }
}

resource "google_logging_metric" "secret_broker_http_request_latency" {
  project = local.foundation.project_id
  name    = "${local.prefix}-secret-broker-http-request-latency"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.secret_broker.name}\"",
    "jsonPayload.metric=\"http_request\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "ms"
    display_name = "Attune secret-broker HTTP request latency"

    labels {
      key         = "route"
      value_type  = "STRING"
      description = "Matched Flask URL rule template; never a raw path or identifier."
    }
    labels {
      key         = "status_class"
      value_type  = "STRING"
      description = "Response status class (2xx..5xx)."
    }
  }

  value_extractor = "EXTRACT(jsonPayload.duration_ms)"
  label_extractors = {
    "route"        = "EXTRACT(jsonPayload.route)"
    "status_class" = "EXTRACT(jsonPayload.status_class)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 24
      growth_factor      = 2.0
      scale              = 5
    }
  }
}

resource "google_monitoring_alert_policy" "secret_broker_5xx_error_rate" {
  project      = local.foundation.project_id
  display_name = "${local.prefix} secret-broker 5xx error rate"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = "The secret broker returned more than ${var.slo_5xx_error_threshold} 5xx responses within ${var.slo_alert_window_seconds} seconds. This is distinct from secret_broker_use_anomaly above (denied/rate-limited/provider-failed credential USE); this alert is transport-layer health. The signal contains no tenant or provider data."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "Secret-broker 5xx responses over threshold"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"logging.googleapis.com/user/${google_logging_metric.secret_broker_http_request_count.name}\"",
        "resource.type=\"cloud_run_revision\"",
        "metric.labels.status_class=\"5xx\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = var.slo_5xx_error_threshold
      duration        = "0s"

      aggregations {
        alignment_period     = "${var.slo_alert_window_seconds}s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = var.alert_notification_channels
  user_labels            = local.common_labels
}

resource "google_logging_metric" "channel_broker_http_request_count" {
  count   = var.enable_channel_broker ? 1 : 0
  project = local.foundation.project_id
  name    = "${local.prefix}-channel-broker-http-request-count"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.channel_broker[0].name}\"",
    "jsonPayload.metric=\"http_request\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Attune channel-broker HTTP request count"

    labels {
      key         = "route"
      value_type  = "STRING"
      description = "Matched Flask URL rule template; never a raw path or identifier."
    }
    labels {
      key         = "method"
      value_type  = "STRING"
      description = "HTTP method."
    }
    labels {
      key         = "status_class"
      value_type  = "STRING"
      description = "Response status class (2xx..5xx)."
    }
  }

  label_extractors = {
    "route"        = "EXTRACT(jsonPayload.route)"
    "method"       = "EXTRACT(jsonPayload.method)"
    "status_class" = "EXTRACT(jsonPayload.status_class)"
  }
}

resource "google_logging_metric" "channel_broker_http_request_latency" {
  count   = var.enable_channel_broker ? 1 : 0
  project = local.foundation.project_id
  name    = "${local.prefix}-channel-broker-http-request-latency"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.channel_broker[0].name}\"",
    "jsonPayload.metric=\"http_request\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "ms"
    display_name = "Attune channel-broker HTTP request latency"

    labels {
      key         = "route"
      value_type  = "STRING"
      description = "Matched Flask URL rule template; never a raw path or identifier."
    }
    labels {
      key         = "status_class"
      value_type  = "STRING"
      description = "Response status class (2xx..5xx)."
    }
  }

  value_extractor = "EXTRACT(jsonPayload.duration_ms)"
  label_extractors = {
    "route"        = "EXTRACT(jsonPayload.route)"
    "status_class" = "EXTRACT(jsonPayload.status_class)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 24
      growth_factor      = 2.0
      scale              = 5
    }
  }
}

resource "google_monitoring_alert_policy" "channel_broker_5xx_error_rate" {
  count        = var.enable_channel_broker ? 1 : 0
  project      = local.foundation.project_id
  display_name = "${local.prefix} channel-broker 5xx error rate"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = "The channel broker returned more than ${var.slo_5xx_error_threshold} 5xx responses within ${var.slo_alert_window_seconds} seconds. Inspect Google Chat/Slack provider health and the broker-egress NAT path; the signal contains no tenant, destination, or provider content."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "Channel-broker 5xx responses over threshold"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"logging.googleapis.com/user/${google_logging_metric.channel_broker_http_request_count[0].name}\"",
        "resource.type=\"cloud_run_revision\"",
        "metric.labels.status_class=\"5xx\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = var.slo_5xx_error_threshold
      duration        = "0s"

      aggregations {
        alignment_period     = "${var.slo_alert_window_seconds}s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = var.alert_notification_channels
  user_labels            = local.common_labels
}
