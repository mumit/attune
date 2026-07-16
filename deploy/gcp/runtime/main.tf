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
  dispatch_broker_audience = "https://${local.prefix}-dispatch-broker.attune.internal"
  secret_broker_audience   = "https://${local.prefix}-secret-broker.attune.internal"
  oauth_exchange_audience  = "https://${local.prefix}-oauth-exchange.attune.internal"
  worker_audience          = "https://${local.prefix}-worker.attune.internal"
  model_gateway_audience   = "https://${local.prefix}-model-gateway.attune.internal"
  audit_callers = toset([
    local.foundation.workload_identities.control_plane,
    local.foundation.workload_identities.channel_broker,
    local.foundation.workload_identities.dispatch_broker,
    local.foundation.workload_identities.secret_broker,
    local.foundation.workload_identities.worker,
  ])
  channel_broker_audience = "https://${local.prefix}-channel-broker.attune.internal"
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
        for_each = var.enable_google_chat_conversation ? [1] : []
        content {
          name  = "ATTUNE_MODEL_GATEWAY_URL"
          value = google_cloud_run_v2_service.model_gateway[0].uri
        }
      }
      dynamic "env" {
        for_each = var.enable_google_chat_conversation ? [1] : []
        content {
          name  = "ATTUNE_MODEL_GATEWAY_AUDIENCE"
          value = local.model_gateway_audience
        }
      }
      dynamic "env" {
        for_each = var.enable_google_chat_conversation ? [1] : []
        content {
          name  = "ATTUNE_CHANNEL_BROKER_URL"
          value = google_cloud_run_v2_service.channel_broker[0].uri
        }
      }
      dynamic "env" {
        for_each = var.enable_google_chat_conversation ? [1] : []
        content {
          name  = "ATTUNE_CHANNEL_BROKER_AUDIENCE"
          value = local.channel_broker_audience
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
  for_each = var.enable_dispatch_broker ? toset([
    local.foundation.workload_identities.control_plane,
    local.foundation.workload_identities.ingress,
    local.foundation.workload_identities.worker,
  ]) : toset([])
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.dispatch_broker[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value}"
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
        tags       = ["attune-channel-broker"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true
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

resource "google_cloud_run_v2_service_iam_member" "channel_broker_control_plane_invoker" {
  count    = var.enable_channel_broker ? 1 : 0
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.channel_broker[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.control_plane}"
}

resource "google_cloud_run_v2_service_iam_member" "channel_broker_worker_invoker" {
  count    = var.enable_channel_broker && var.enable_google_chat_conversation ? 1 : 0
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
