data "terraform_remote_state" "foundation" {
  backend = "gcs"
  config = {
    bucket = var.state_bucket
    prefix = var.foundation_state_prefix
  }
}

data "terraform_remote_state" "runtime" {
  backend = "gcs"
  config = {
    bucket = var.state_bucket
    prefix = var.runtime_state_prefix
  }
}

locals {
  foundation = data.terraform_remote_state.foundation.outputs.foundation
  runtime    = data.terraform_remote_state.runtime.outputs
  prefix     = "attune-${local.foundation.environment}"
  labels = merge(
    {
      application = "attune"
      environment = local.foundation.environment
      managed_by  = "terraform"
      component   = "control-plane-edge"
    },
    var.labels,
  )
}

check "google_chat_ingress_activation" {
  assert {
    condition = !var.enable_google_chat_ingress || (
      var.deploy_google_chat_ingress && var.google_chat_provider_ready
    )
    error_message = "Google Chat route activation requires deployed ingress and provider-readiness attestation."
  }
}

check "google_chat_conversation_activation" {
  assert {
    condition = !var.enable_google_chat_conversation || (
      var.enable_google_chat_ingress &&
      try(local.runtime.google_chat_conversation_enabled, false) &&
      local.runtime.dispatch_broker != null
    )
    error_message = "Google Chat conversation ingress requires the routed provider endpoint and the activated runtime conversation route."
  }
}

check "slack_ingress_activation" {
  assert {
    condition = !var.enable_slack_ingress || (
      var.deploy_slack_ingress && var.slack_provider_ready
    )
    error_message = "Slack route activation requires deployed ingress and provider-readiness attestation."
  }
}

check "slack_conversation_activation" {
  assert {
    condition = !var.enable_slack_conversation || (
      var.enable_slack_ingress &&
      try(local.runtime.slack_channel_enabled, false) &&
      local.runtime.dispatch_broker != null
    )
    error_message = "Slack conversation ingress requires the routed provider endpoint, the configured broker Slack routes, and the fixed dispatch broker."
  }
}

check "customer_export_activation" {
  assert {
    condition = !var.enable_customer_exports || (
      var.deploy_customer_export_download &&
      var.enable_identity_sign_in &&
      try(local.runtime.export_writer != null, false) &&
      local.runtime.dispatch_broker != null
    )
    error_message = "Customer exports require identity, the private export writer, and the dispatch broker."
  }
}

resource "google_cloud_run_v2_service" "control_plane" {
  project              = local.foundation.project_id
  name                 = "${local.prefix}-control-plane"
  location             = local.foundation.region
  deletion_protection  = true
  ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  default_uri_disabled = true
  invoker_iam_disabled = true
  labels               = local.labels

  template {
    service_account                  = local.foundation.workload_identities.control_plane
    timeout                          = "10s"
    max_instance_request_concurrency = 20

    scaling {
      min_instance_count = var.enable_google_workspace_oauth ? 1 : 0
      max_instance_count = 2
    }

    containers {
      name  = "control-plane"
      image = var.control_plane_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "256Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "ATTUNE_PUBLIC_HOST"
        value = var.hostname
      }
      env {
        name  = "ATTUNE_IDENTITY_ENABLED"
        value = tostring(var.enable_identity_sign_in)
      }
      env {
        name  = "ATTUNE_IDENTITY_PROJECT"
        value = local.foundation.project_id
      }
      env {
        name  = "ATTUNE_GOOGLE_OAUTH_ENABLED"
        value = tostring(var.enable_google_workspace_oauth)
      }
      env {
        name  = "ATTUNE_GOOGLE_CONNECTION_TEST_ENABLED"
        value = tostring(local.runtime.google_workspace_verification_enabled)
      }
      env {
        name  = "ATTUNE_HOSTED_ONBOARDING_ENABLED"
        value = tostring(var.enable_hosted_onboarding)
      }
      env {
        name  = "ATTUNE_HOSTED_POLICY_ENABLED"
        value = tostring(var.enable_hosted_policy)
      }
      env {
        name  = "ATTUNE_HOSTED_CHANNELS_ENABLED"
        value = tostring(var.enable_hosted_channels)
      }
      env {
        name  = "ATTUNE_HOSTED_CHANNEL_SETUP_ENABLED"
        value = tostring(var.enable_hosted_channel_setup)
      }
      env {
        name  = "ATTUNE_HOSTED_CHANNEL_LIFECYCLE_ENABLED"
        value = tostring(var.enable_hosted_channel_lifecycle)
      }
      env {
        name  = "ATTUNE_CUSTOMER_EXPORTS_ENABLED"
        value = tostring(var.enable_customer_exports)
      }
      env {
        name  = "ATTUNE_HOSTED_SLACK_INSTALL_ENABLED"
        value = tostring(var.enable_hosted_slack_install)
      }
      env {
        name  = "ATTUNE_HOSTED_WEB_CONVERSATION_ENABLED"
        value = tostring(var.enable_hosted_web_conversation)
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
      env {
        name  = "ATTUNE_HOSTED_SIGNUP_ENABLED"
        value = tostring(var.enable_hosted_signup)
      }
      dynamic "env" {
        for_each = var.enable_hosted_signup ? [1] : []
        content {
          name  = "ATTUNE_HOSTED_SIGNUP_REGION"
          value = var.hosted_signup_region
        }
      }
      env {
        name  = "ATTUNE_HOSTED_DELETION_ENABLED"
        value = tostring(var.enable_hosted_deletion)
      }
      dynamic "env" {
        for_each = var.enable_hosted_slack_install ? [1] : []
        content {
          name  = "ATTUNE_SLACK_CLIENT_ID"
          value = var.slack_client_id
        }
      }
      dynamic "env" {
        for_each = var.enable_hosted_channel_setup ? [1] : []
        content {
          name  = "ATTUNE_CHANNEL_BROKER_URL"
          value = local.runtime.channel_broker.uri
        }
      }
      dynamic "env" {
        for_each = var.enable_hosted_channel_setup ? [1] : []
        content {
          name  = "ATTUNE_CHANNEL_BROKER_AUDIENCE"
          value = local.runtime.channel_broker.audience
        }
      }
      dynamic "env" {
        for_each = var.enable_hosted_policy || var.enable_hosted_channels || var.enable_hosted_channel_setup || var.enable_hosted_web_conversation || var.enable_hosted_signup || var.enable_hosted_deletion || var.enable_tenant_model_profiles ? [1] : []
        content {
          name  = "ATTUNE_AUDIT_WRITER_URL"
          value = local.runtime.audit_writer.uri
        }
      }
      dynamic "env" {
        for_each = local.runtime.google_workspace_verification_enabled || var.enable_customer_exports || var.enable_hosted_web_conversation || var.enable_hosted_brief ? [1] : []
        content {
          name  = "ATTUNE_DISPATCH_BROKER_URL"
          value = local.runtime.dispatch_broker.uri
        }
      }
      dynamic "env" {
        for_each = local.runtime.google_workspace_verification_enabled || var.enable_customer_exports || var.enable_hosted_web_conversation || var.enable_hosted_brief ? [1] : []
        content {
          name  = "ATTUNE_DISPATCH_BROKER_AUDIENCE"
          value = local.runtime.dispatch_broker.audience
        }
      }
      dynamic "env" {
        for_each = var.enable_google_workspace_oauth ? [1] : []
        content {
          name  = "ATTUNE_GOOGLE_OAUTH_CLIENT_ID"
          value = var.google_oauth_client_id
        }
      }
      dynamic "env" {
        for_each = var.enable_google_workspace_oauth ? [1] : []
        content {
          name  = "ATTUNE_SECRET_BROKER_URL"
          value = local.runtime.secret_broker.uri
        }
      }
      dynamic "env" {
        for_each = var.enable_google_workspace_oauth ? [1] : []
        content {
          name  = "ATTUNE_SECRET_BROKER_AUDIENCE"
          value = local.runtime.secret_broker.audience
        }
      }
      dynamic "env" {
        for_each = var.enable_identity_sign_in ? [1] : []
        content {
          name  = "ATTUNE_IDENTITY_API_KEY"
          value = var.identity_api_key
        }
      }
      dynamic "env" {
        for_each = var.enable_identity_sign_in ? [1] : []
        content {
          name  = "ATTUNE_IDENTITY_AUTH_DOMAIN"
          value = "${local.foundation.project_id}.firebaseapp.com"
        }
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
          local.foundation.workload_identities.control_plane,
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
          http_headers {
            name  = "Host"
            value = var.hostname
          }
        }
      }

      liveness_probe {
        timeout_seconds   = 2
        period_seconds    = 10
        failure_threshold = 3
        http_get {
          path = "/healthz"
          port = 8080
          http_headers {
            name  = "Host"
            value = var.hostname
          }
        }
      }
    }

    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = local.foundation.network_id
        subnetwork = local.foundation.subnetwork_id
        tags       = ["attune-control-plane"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition = !var.enable_identity_sign_in || (
        var.identity_provider_ready && var.identity_api_key != ""
      )
      error_message = "Identity sign-in activation requires provider-readiness attestation and the public browser API key."
    }
    precondition {
      condition = !var.enable_google_workspace_oauth || (
        var.enable_identity_sign_in &&
        var.google_oauth_provider_ready &&
        var.google_oauth_client_id != ""
      )
      error_message = "Google Workspace OAuth activation requires identity sign-in, provider-readiness attestation, and the separate public client ID."
    }
    precondition {
      condition = !local.runtime.google_workspace_verification_enabled || (
        var.enable_google_workspace_oauth && local.runtime.dispatch_broker != null
      )
      error_message = "The browser connection test requires active Workspace OAuth and the fixed dispatch broker."
    }
    precondition {
      condition     = !var.enable_hosted_onboarding || var.enable_identity_sign_in
      error_message = "Hosted onboarding requires active identity sign-in."
    }
    precondition {
      condition     = !var.enable_hosted_policy || var.enable_hosted_onboarding
      error_message = "Hosted policy review requires active hosted onboarding."
    }
    precondition {
      condition     = !var.enable_hosted_channels || var.enable_hosted_onboarding
      error_message = "Hosted channel preferences require active hosted onboarding."
    }
    precondition {
      condition = !var.enable_hosted_channel_setup || (
        var.enable_hosted_channels && local.runtime.channel_broker != null
      )
      error_message = "Hosted channel setup requires active hosted channel preferences and the private channel broker."
    }
    precondition {
      condition     = !var.enable_hosted_channel_lifecycle || var.enable_hosted_channel_setup
      error_message = "Hosted channel lifecycle requires active hosted channel setup."
    }
    precondition {
      condition     = !var.enable_hosted_slack_install || var.slack_client_id != ""
      error_message = "Hosted Slack installation requires the platform Slack app's public client ID."
    }
    precondition {
      condition = !var.enable_hosted_signup || (
        var.enable_identity_sign_in && var.hosted_signup_region != ""
      )
      error_message = "Hosted signup activation requires identity sign-in and the fixed signup region."
    }
    precondition {
      condition     = !var.enable_hosted_deletion || var.enable_identity_sign_in
      error_message = "Hosted tenant-deletion routes require active identity sign-in."
    }
    precondition {
      condition = !var.enable_hosted_brief || (
        var.enable_identity_sign_in && try(local.runtime.hosted_brief_enabled, false)
      )
      error_message = "Hosted brief activation requires identity sign-in and the activated runtime worker brief route."
    }
    precondition {
      condition = !var.enable_tenant_model_profiles || (
        var.enable_identity_sign_in && try(local.runtime.tenant_model_profiles_enabled, false)
      )
      error_message = "Hosted model profile activation requires identity sign-in and the activated runtime worker/model-gateway profile route."
    }
    precondition {
      condition     = !var.enable_model_usage_metering || var.enable_identity_sign_in
      error_message = "Hosted usage activation requires active identity sign-in. Independently activatable from the worker's own metering-write gate."
    }
  }
}

# Cloud Run and Cloud Armor request logs include the full callback URL. Exclude
# both request-log planes for the dedicated callback service/backend by resource
# identity, without inspecting or matching the credential-bearing URL itself.
resource "google_logging_project_exclusion" "oauth_callback_requests" {
  project     = local.foundation.project_id
  name        = "${local.prefix}-oauth-callback-requests"
  description = "Never retain credential-bearing OAuth callback request URLs"
  filter      = <<-EOT
    (resource.type="cloud_run_revision"
      AND resource.labels.service_name="${local.prefix}-oauth-callback"
      AND log_id("run.googleapis.com/requests"))
    OR
    (resource.type="http_load_balancer"
      AND resource.labels.backend_service_name="${local.prefix}-oauth-callback"
      AND log_id("requests"))
  EOT

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_cloud_run_v2_service" "oauth_callback" {
  project              = local.foundation.project_id
  name                 = "${local.prefix}-oauth-callback"
  location             = local.foundation.region
  deletion_protection  = true
  ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  default_uri_disabled = true
  invoker_iam_disabled = true
  labels               = merge(local.labels, { component = "oauth-callback" })

  template {
    service_account                  = local.foundation.workload_identities.oauth_callback
    timeout                          = "10s"
    max_instance_request_concurrency = 20

    scaling {
      min_instance_count = var.enable_google_workspace_oauth ? 1 : 0
      max_instance_count = 2
    }

    containers {
      name  = "oauth-callback"
      image = var.oauth_callback_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "256Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "ATTUNE_PUBLIC_HOST"
        value = var.hostname
      }
      env {
        name  = "ATTUNE_GOOGLE_OAUTH_ENABLED"
        value = tostring(var.enable_google_workspace_oauth)
      }
      dynamic "env" {
        for_each = var.enable_google_workspace_oauth ? [1] : []
        content {
          name  = "ATTUNE_OAUTH_EXCHANGE_URL"
          value = local.runtime.oauth_exchange.uri
        }
      }
      dynamic "env" {
        for_each = var.enable_google_workspace_oauth ? [1] : []
        content {
          name  = "ATTUNE_OAUTH_EXCHANGE_AUDIENCE"
          value = local.runtime.oauth_exchange.audience
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
          http_headers {
            name  = "Host"
            value = var.hostname
          }
        }
      }

      liveness_probe {
        timeout_seconds   = 2
        period_seconds    = 10
        failure_threshold = 3
        http_get {
          path = "/healthz"
          port = 8080
          http_headers {
            name  = "Host"
            value = var.hostname
          }
        }
      }
    }

    # All egress enters the no-NAT application subnet. The dormant scrubber
    # makes no outbound calls.
    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = local.foundation.network_id
        subnetwork = local.foundation.subnetwork_id
        tags       = ["attune-oauth-callback"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition = !var.enable_google_workspace_oauth || (
        var.google_oauth_provider_ready &&
        var.google_oauth_client_id != ""
      )
      error_message = "OAuth callback activation requires provider-readiness attestation and the separate Workspace client ID."
    }
  }

  depends_on = [google_logging_project_exclusion.oauth_callback_requests]
}

resource "google_cloud_run_v2_service" "google_chat_ingress" {
  count                = var.deploy_google_chat_ingress ? 1 : 0
  project              = local.foundation.project_id
  name                 = "${local.prefix}-google-chat-ingress"
  location             = local.foundation.region
  deletion_protection  = true
  ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  default_uri_disabled = true
  invoker_iam_disabled = true
  labels               = merge(local.labels, { component = "google-chat-ingress" })

  template {
    service_account                  = local.foundation.workload_identities.ingress
    timeout                          = "15s"
    max_instance_request_concurrency = 8

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      name  = "google-chat-ingress"
      image = var.google_chat_ingress_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "256Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "ATTUNE_GOOGLE_CHAT_AUDIENCE"
        value = "https://${var.hostname}/v1/provider/google-chat/events"
      }
      env {
        name  = "ATTUNE_GOOGLE_CHAT_PROJECT_NUMBER"
        value = var.google_chat_project_number
      }
      env {
        name = "ATTUNE_CHANNEL_BROKER_URL"
        value = (
          local.runtime.channel_broker == null
          ? ""
          : local.runtime.channel_broker.uri
        )
      }
      env {
        name = "ATTUNE_CHANNEL_BROKER_AUDIENCE"
        value = (
          local.runtime.channel_broker == null
          ? ""
          : local.runtime.channel_broker.audience
        )
      }
      env {
        name  = "ATTUNE_ENABLE_GOOGLE_CHAT_CONVERSATION"
        value = tostring(var.enable_google_chat_conversation)
      }
      dynamic "env" {
        for_each = var.enable_google_chat_conversation ? [1] : []
        content {
          name  = "ATTUNE_DISPATCH_BROKER_URL"
          value = local.runtime.dispatch_broker.uri
        }
      }
      dynamic "env" {
        for_each = var.enable_google_chat_conversation ? [1] : []
        content {
          name  = "ATTUNE_DISPATCH_BROKER_AUDIENCE"
          value = local.runtime.dispatch_broker.audience
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
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = local.foundation.network_id
        subnetwork = local.foundation.subnetwork_id
        tags       = ["attune-google-chat-ingress"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition = !var.deploy_google_chat_ingress || (
        local.runtime.channel_broker != null &&
        var.google_chat_project_number != ""
      )
      error_message = "Google Chat ingress deployment requires the private channel broker and Chat project number."
    }

    precondition {
      condition = !var.enable_google_chat_ingress || (
        var.deploy_google_chat_ingress && var.google_chat_provider_ready
      )
      error_message = "Google Chat route activation requires deployed ingress and provider-readiness attestation."
    }

    precondition {
      condition = !var.enable_google_chat_conversation || (
        var.enable_google_chat_ingress &&
        try(local.runtime.google_chat_conversation_enabled, false) &&
        local.runtime.dispatch_broker != null
      )
      error_message = "Google Chat conversation activation requires the routed ingress and activated runtime conversation route."
    }
  }
}

resource "google_cloud_run_v2_service" "slack_ingress" {
  count                = var.deploy_slack_ingress ? 1 : 0
  project              = local.foundation.project_id
  name                 = "${local.prefix}-slack-ingress"
  location             = local.foundation.region
  deletion_protection  = true
  ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  default_uri_disabled = true
  invoker_iam_disabled = true
  labels               = merge(local.labels, { component = "slack-ingress" })

  template {
    service_account                  = local.foundation.workload_identities.slack_ingress
    timeout                          = "15s"
    max_instance_request_concurrency = 8

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      name  = "slack-ingress"
      image = var.slack_ingress_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "256Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name = "ATTUNE_CHANNEL_BROKER_URL"
        value = (
          local.runtime.channel_broker == null
          ? ""
          : local.runtime.channel_broker.uri
        )
      }
      env {
        name = "ATTUNE_CHANNEL_BROKER_AUDIENCE"
        value = (
          local.runtime.channel_broker == null
          ? ""
          : local.runtime.channel_broker.audience
        )
      }
      env {
        name  = "ATTUNE_SLACK_SIGNING_SECRET"
        value = local.foundation.platform_secret_ids["slack-signing-secret"]
      }
      env {
        name  = "ATTUNE_ENABLE_SLACK_CONVERSATION"
        value = tostring(var.enable_slack_conversation)
      }
      dynamic "env" {
        for_each = var.enable_slack_conversation ? [1] : []
        content {
          name  = "ATTUNE_DISPATCH_BROKER_URL"
          value = local.runtime.dispatch_broker.uri
        }
      }
      dynamic "env" {
        for_each = var.enable_slack_conversation ? [1] : []
        content {
          name  = "ATTUNE_DISPATCH_BROKER_AUDIENCE"
          value = local.runtime.dispatch_broker.audience
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
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = local.foundation.network_id
        subnetwork = local.foundation.subnetwork_id
        tags       = ["attune-slack-ingress"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition = !var.deploy_slack_ingress || (
        local.runtime.channel_broker != null &&
        var.slack_ingress_image != ""
      )
      error_message = "Slack ingress deployment requires the private channel broker and a pinned ingress image."
    }

    precondition {
      condition = !var.enable_slack_ingress || (
        var.deploy_slack_ingress && var.slack_provider_ready
      )
      error_message = "Slack route activation requires deployed ingress and provider-readiness attestation."
    }

    precondition {
      condition = !var.enable_slack_conversation || (
        var.enable_slack_ingress &&
        try(local.runtime.slack_channel_enabled, false) &&
        local.runtime.dispatch_broker != null
      )
      error_message = "Slack conversation activation requires the routed ingress, the configured broker Slack routes, and the fixed dispatch broker."
    }
  }
}

resource "google_compute_region_network_endpoint_group" "control_plane" {
  project               = local.foundation.project_id
  name                  = "${local.prefix}-control-plane"
  region                = local.foundation.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.control_plane.name
  }
}

resource "google_cloud_run_v2_service" "export_download" {
  count                = var.deploy_customer_export_download ? 1 : 0
  project              = local.foundation.project_id
  name                 = "${local.prefix}-export-download"
  location             = local.foundation.region
  deletion_protection  = true
  ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  default_uri_disabled = true
  invoker_iam_disabled = true
  labels               = merge(local.labels, { component = "export-download" })
  template {
    service_account                  = local.foundation.workload_identities.export_download
    timeout                          = "120s"
    max_instance_request_concurrency = 1
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      name  = "export-download"
      image = var.export_download_image
      ports { container_port = 8080 }
      resources {
        limits            = { cpu = "1", memory = "256Mi" }
        cpu_idle          = true
        startup_cpu_boost = true
      }
      env {
        name  = "ATTUNE_PUBLIC_HOST"
        value = var.hostname
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
        name  = "ATTUNE_DB_USER"
        value = trimsuffix(local.foundation.workload_identities.export_download, ".gserviceaccount.com")
      }
      env {
        name  = "ATTUNE_EXPORT_BUCKET"
        value = local.foundation.customer_export_bucket
      }
      env {
        name  = "ATTUNE_EXPORT_KMS_KEY"
        value = local.foundation.customer_export_kms_key
      }
      startup_probe {
        initial_delay_seconds = 1
        timeout_seconds       = 2
        period_seconds        = 3
        failure_threshold     = 10
        http_get {
          path = "/healthz"
          port = 8080
          http_headers {
            name  = "Host"
            value = var.hostname
          }
        }
      }
      liveness_probe {
        timeout_seconds   = 2
        period_seconds    = 10
        failure_threshold = 3
        http_get {
          path = "/healthz"
          port = 8080
          http_headers {
            name  = "Host"
            value = var.hostname
          }
        }
      }
    }
    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = local.foundation.network_id
        subnetwork = local.foundation.subnetwork_id
        tags       = ["attune-export-download"]
      }
    }
  }
  lifecycle { prevent_destroy = true }
}

resource "google_compute_region_network_endpoint_group" "export_download" {
  count                 = var.deploy_customer_export_download ? 1 : 0
  project               = local.foundation.project_id
  name                  = "${local.prefix}-export-download"
  region                = local.foundation.region
  network_endpoint_type = "SERVERLESS"
  cloud_run { service = google_cloud_run_v2_service.export_download[0].name }
}

resource "google_compute_region_network_endpoint_group" "oauth_callback" {
  project               = local.foundation.project_id
  name                  = "${local.prefix}-oauth-callback"
  region                = local.foundation.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.oauth_callback.name
  }
}

resource "google_compute_region_network_endpoint_group" "google_chat_ingress" {
  count                 = var.deploy_google_chat_ingress ? 1 : 0
  project               = local.foundation.project_id
  name                  = "${local.prefix}-google-chat-ingress"
  region                = local.foundation.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.google_chat_ingress[0].name
  }
}

resource "google_compute_region_network_endpoint_group" "slack_ingress" {
  count                 = var.deploy_slack_ingress ? 1 : 0
  project               = local.foundation.project_id
  name                  = "${local.prefix}-slack-ingress"
  region                = local.foundation.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.slack_ingress[0].name
  }
}

resource "google_compute_security_policy" "edge" {
  project     = local.foundation.project_id
  name        = "${local.prefix}-control-plane-edge"
  description = "Exact-host and bounded-rate policy for the locked Attune edge"
  type        = "CLOUD_ARMOR"

  dynamic "rule" {
    for_each = var.enable_google_workspace_oauth ? [1] : []
    content {
      action      = "throttle"
      priority    = 880
      description = "Permit only the authenticated Google connector start path"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.path == '/v1/connectors/google/start'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 10
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_hosted_onboarding ? [1] : []
    content {
      action      = "throttle"
      priority    = 884
      description = "Permit only authenticated hosted onboarding state paths"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && (request.path == '/v1/onboarding' || request.path == '/v1/onboarding/start')"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 30
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_hosted_policy ? [1] : []
    content {
      action      = "throttle"
      priority    = 885
      description = "Permit only authenticated hosted policy review and confirmation"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && (request.path == '/v1/onboarding/policy' || request.path == '/v1/onboarding/policy/confirm')"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 10
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_hosted_channels ? [1] : []
    content {
      action      = "throttle"
      priority    = 886
      description = "Permit only authenticated hosted channel preference paths"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.path == '/v1/onboarding/channels'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 10
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_hosted_channel_setup ? [1] : []
    content {
      action      = "throttle"
      priority    = 887
      description = "Permit only authenticated hosted channel installation setup paths"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && (request.path == '/v1/onboarding/channel-installations' || request.path == '/v1/onboarding/channel-installations/google-chat/link' || request.path == '/v1/onboarding/channel-installations/google-chat/test')"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 10
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_hosted_channel_lifecycle ? [1] : []
    content {
      action      = "throttle"
      priority    = 888
      description = "Permit only recent-authenticated hosted Google Chat disconnection"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.path == '/v1/onboarding/channel-installations/google-chat'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 5
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_hosted_slack_install ? [1] : []
    content {
      action      = "throttle"
      priority    = 891
      description = "Permit only authenticated hosted Slack installation setup paths"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && (request.path == '/v1/onboarding/channel-installations/slack/install' || request.path == '/v1/onboarding/channel-installations/slack/callback' || request.path == '/v1/onboarding/channel-installations/slack/test')"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 10
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_hosted_channel_lifecycle && var.enable_hosted_slack_install ? [1] : []
    content {
      action      = "throttle"
      priority    = 892
      description = "Permit only recent-authenticated hosted Slack disconnection"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.path == '/v1/onboarding/channel-installations/slack'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 5
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_hosted_web_conversation ? [1] : []
    content {
      action      = "throttle"
      priority    = 893
      description = "Permit only authenticated hosted web conversation message and turn-poll paths"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && (request.path == '/v1/conversation/messages' || request.path == '/v1/conversation/turns')"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        # 60/60s (vs. the 10/60s onboarding-ceremony rules) tolerates a
        # browser tab polling turns every 2 seconds.
        rate_limit_threshold {
          count        = 60
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_customer_exports ? [1] : []
    content {
      action      = "throttle"
      priority    = 889
      description = "Permit only authenticated customer export request, status, and authorization paths"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && (request.path == '/v1/exports' || request.path.matches('^/v1/exports/[0-9a-f-]{36}/download-authorizations$'))"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 30
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = local.runtime.google_workspace_verification_enabled ? [1] : []
    content {
      action      = "throttle"
      priority    = 881
      description = "Permit only the authenticated fixed Google connection test"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.path == '/v1/connectors/google/test'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 10
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_google_workspace_oauth ? [1] : []
    content {
      action      = "throttle"
      priority    = 883
      description = "Permit only authenticated Google connector disconnection"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.path == '/v1/connectors/google'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 10
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = local.runtime.google_workspace_verification_enabled ? [1] : []
    content {
      action      = "throttle"
      priority    = 882
      description = "Permit bounded polling of an opaque Google connection test"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.path.startsWith('/v1/connectors/google/tests/')"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 60
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_identity_sign_in ? [1] : []
    content {
      action      = "throttle"
      priority    = 890
      description = "Permit only staged identity configuration and fixed assets"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.method == 'GET' && (request.path == '/v1/identity/config' || request.path == '/assets/attune.css' || request.path == '/assets/identity.js' || request.path == '/assets/attune-chat-avatar.png')"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 30
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_hosted_signup ? [1] : []
    content {
      action      = "throttle"
      # Reserved by docs/hosted-signup.md section 7: "the next free priority
      # in the reviewed range is 894."
      priority    = 894
      description = "Permit only the exact sessionless self-service signup ceremony"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.path == '/v1/signup'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 10
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_hosted_deletion ? [1] : []
    content {
      action      = "throttle"
      priority    = 895
      description = "Permit bounded reads of an owner's own tenant-deletion request state"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.method == 'GET' && request.path == '/v1/account/deletion-request'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 30
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_hosted_deletion ? [1] : []
    content {
      action      = "throttle"
      priority    = 896
      description = "Permit only recent-authenticated tenant-deletion request/cancel ceremony"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.path == '/v1/account/deletion-requests'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        # 5/60s matches the two other recent-authenticated destructive
        # ceremonies already in this policy (Google Chat disconnect at 888,
        # Slack disconnect at 892) -- account deletion is at least as severe.
        rate_limit_threshold {
          count        = 5
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_tenant_model_profiles ? [1] : []
    content {
      action      = "throttle"
      priority    = 897
      description = "Permit only the exact model-profile read/configure path"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.path == '/v1/model-profile'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        # PUT here is an ordinary owner preference (same authorization bar
        # as POST /v1/conversation/messages and POST /v1/brief/run per
        # control_plane_service.py's own docstring), not a recency ceremony,
        # but the route class is still unspecified upstream -- the
        # conservative ceremony rate applies (docs/hosted-model-profiles.md
        # leaves the exact Cloud Armor rule to this Terraform wiring).
        rate_limit_threshold {
          count        = 10
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_model_usage_metering ? [1] : []
    content {
      action      = "throttle"
      priority    = 898
      description = "Permit only the exact bounded usage-summary read"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.path == '/v1/usage'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 30
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_hosted_brief ? [1] : []
    content {
      action      = "throttle"
      priority    = 899
      description = "Permit only the exact idempotent-per-hour brief-run trigger"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.path == '/v1/brief/run'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        # Not a recency ceremony either (same docstring precedent as
        # /v1/model-profile above); the conservative ceremony rate applies
        # for the same reason.
        rate_limit_threshold {
          count        = 10
          interval_sec = 60
        }
      }
    }
  }

  dynamic "rule" {
    for_each = var.enable_identity_sign_in ? [1] : []
    content {
      action      = "throttle"
      priority    = 900
      description = "Permit only staged session paths; application enforces methods"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && (request.path == '/v1/session/bootstrap' || request.path == '/v1/session')"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 30
          interval_sec = 60
        }
      }
    }
  }

  rule {
    action      = "throttle"
    priority    = 1000
    description = "Permit only locked-shell paths on the exact development host"
    match {
      expr {
        expression = "request.headers['host'] == '${var.hostname}' && (request.path == '/' || request.path == '/healthz')"
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 60
        interval_sec = 60
      }
    }
  }

  rule {
    action      = "deny(403)"
    priority    = 2147483647
    description = "Default deny"
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}

resource "google_compute_security_policy" "oauth_callback" {
  project     = local.foundation.project_id
  name        = "${local.prefix}-oauth-callback-edge"
  description = "Exact dormant OAuth callback route with bounded source rate"
  type        = "CLOUD_ARMOR"

  rule {
    action      = "throttle"
    priority    = 1000
    description = "Permit only GET on the exact Google callback and host"
    match {
      expr {
        expression = "request.headers['host'] == '${var.hostname}' && request.method == 'GET' && request.path == '/oauth/google/callback'"
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 20
        interval_sec = 60
      }
    }
  }

  rule {
    action      = "deny(403)"
    priority    = 2147483647
    description = "Default deny"
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}

resource "google_compute_security_policy" "export_download" {
  count       = var.deploy_customer_export_download ? 1 : 0
  project     = local.foundation.project_id
  name        = "${local.prefix}-export-download-edge"
  description = "Exact same-origin one-time export download endpoint"
  type        = "CLOUD_ARMOR"
  dynamic "rule" {
    for_each = var.enable_customer_exports ? [1] : []
    content {
      action      = "throttle"
      priority    = 1000
      description = "Permit only POST to the fixed export download endpoint"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.method == 'POST' && request.path == '/v1/export-download'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 10
          interval_sec = 60
        }
      }
    }
  }
  rule {
    action      = "deny(403)"
    priority    = 2147483647
    description = "Default deny"
    match {
      versioned_expr = "SRC_IPS_V1"
      config { src_ip_ranges = ["*"] }
    }
  }
}

resource "google_compute_security_policy" "google_chat_ingress" {
  count       = var.deploy_google_chat_ingress ? 1 : 0
  project     = local.foundation.project_id
  name        = "${local.prefix}-google-chat-ingress-edge"
  description = "Exact verified Google Chat event route with bounded source rate"
  type        = "CLOUD_ARMOR"

  dynamic "rule" {
    for_each = var.enable_google_chat_ingress && var.deploy_google_chat_ingress ? [1] : []
    content {
      action      = "throttle"
      priority    = 1000
      description = "Permit only POST on the exact Google Chat event endpoint"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.method == 'POST' && request.path == '/v1/provider/google-chat/events'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 60
          interval_sec = 60
        }
      }
    }
  }

  rule {
    action      = "deny(403)"
    priority    = 2147483647
    description = "Default deny"
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}

resource "google_compute_security_policy" "slack_ingress" {
  count       = var.deploy_slack_ingress ? 1 : 0
  project     = local.foundation.project_id
  name        = "${local.prefix}-slack-ingress-edge"
  description = "Exact verified Slack event route with bounded source rate"
  type        = "CLOUD_ARMOR"

  dynamic "rule" {
    for_each = var.enable_slack_ingress && var.deploy_slack_ingress ? [1] : []
    content {
      action      = "throttle"
      priority    = 1000
      description = "Permit only POST on the exact Slack event endpoint"
      match {
        expr {
          expression = "request.headers['host'] == '${var.hostname}' && request.method == 'POST' && request.path == '/v1/provider/slack/events'"
        }
      }
      rate_limit_options {
        conform_action = "allow"
        exceed_action  = "deny(429)"
        enforce_on_key = "IP"
        rate_limit_threshold {
          count        = 60
          interval_sec = 60
        }
      }
    }
  }

  rule {
    action      = "deny(403)"
    priority    = 2147483647
    description = "Default deny"
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}

resource "google_compute_backend_service" "control_plane" {
  project               = local.foundation.project_id
  name                  = "${local.prefix}-control-plane"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  timeout_sec           = 30
  security_policy       = google_compute_security_policy.edge.id

  backend {
    group = google_compute_region_network_endpoint_group.control_plane.id
  }

  log_config {
    enable      = true
    sample_rate = 1
  }
}

resource "google_compute_backend_service" "oauth_callback" {
  project               = local.foundation.project_id
  name                  = "${local.prefix}-oauth-callback"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.oauth_callback.id

  backend {
    group = google_compute_region_network_endpoint_group.oauth_callback.id
  }

  # This must remain disabled: callback URLs carry authorization codes.
  log_config {
    enable = false
  }
}

resource "google_compute_backend_service" "export_download" {
  count                 = var.deploy_customer_export_download ? 1 : 0
  project               = local.foundation.project_id
  name                  = "${local.prefix}-export-download"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  timeout_sec           = 120
  security_policy       = google_compute_security_policy.export_download[0].id
  backend { group = google_compute_region_network_endpoint_group.export_download[0].id }
  # The request body carries a one-time bearer; disable load-balancer request logs.
  log_config { enable = false }
}

resource "google_logging_metric" "export_download_failure" {
  count   = var.deploy_customer_export_download ? 1 : 0
  project = local.foundation.project_id
  name    = "${local.prefix}-export-download-failure"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.export_download[0].name}\"",
    "httpRequest.status>=500",
  ])
  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Attune customer-export download failures"
  }
}

resource "google_monitoring_alert_policy" "export_download_failure" {
  count        = var.deploy_customer_export_download ? 1 : 0
  project      = local.foundation.project_id
  display_name = "${local.prefix} customer-export download failure"
  combiner     = "OR"
  enabled      = true
  documentation {
    content   = "A one-time customer-export read, authenticated decryption, or consumption failed. Inspect without logging or replaying the bearer secret."
    mime_type = "text/markdown"
  }
  conditions {
    display_name = "At least one export download 5xx"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.export_download_failure[0].name}\" AND resource.type=\"cloud_run_revision\""
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
  user_labels           = local.labels
}

resource "google_compute_backend_service" "google_chat_ingress" {
  count                 = var.deploy_google_chat_ingress ? 1 : 0
  project               = local.foundation.project_id
  name                  = "${local.prefix}-google-chat-ingress"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.google_chat_ingress[0].id

  backend {
    group = google_compute_region_network_endpoint_group.google_chat_ingress[0].id
  }

  log_config {
    enable = false
  }
}

resource "google_compute_backend_service" "slack_ingress" {
  count                 = var.deploy_slack_ingress ? 1 : 0
  project               = local.foundation.project_id
  name                  = "${local.prefix}-slack-ingress"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.slack_ingress[0].id

  backend {
    group = google_compute_region_network_endpoint_group.slack_ingress[0].id
  }

  log_config {
    enable = false
  }
}

resource "google_compute_global_address" "edge" {
  project      = local.foundation.project_id
  name         = "${local.prefix}-control-plane-edge"
  address_type = "EXTERNAL"
  ip_version   = "IPV4"
}

resource "google_compute_managed_ssl_certificate" "edge" {
  project = local.foundation.project_id
  name    = "${local.prefix}-control-plane-edge"

  managed {
    domains = [var.hostname]
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_ssl_policy" "edge" {
  project         = local.foundation.project_id
  name            = "${local.prefix}-control-plane-edge"
  profile         = "MODERN"
  min_tls_version = "TLS_1_2"
}

resource "google_compute_url_map" "https" {
  project         = local.foundation.project_id
  name            = "${local.prefix}-control-plane-https"
  default_service = google_compute_backend_service.control_plane.id

  host_rule {
    hosts        = [var.hostname]
    path_matcher = "attune-public-host"
  }

  path_matcher {
    name            = "attune-public-host"
    default_service = google_compute_backend_service.control_plane.id

    path_rule {
      paths   = ["/oauth/google/callback"]
      service = google_compute_backend_service.oauth_callback.id
    }

    dynamic "path_rule" {
      for_each = var.enable_customer_exports ? [1] : []
      content {
        paths   = ["/v1/export-download"]
        service = google_compute_backend_service.export_download[0].id
      }
    }

    dynamic "path_rule" {
      for_each = var.enable_google_chat_ingress && var.deploy_google_chat_ingress ? [1] : []
      content {
        paths   = ["/v1/provider/google-chat/events"]
        service = google_compute_backend_service.google_chat_ingress[0].id
      }
    }

    dynamic "path_rule" {
      for_each = var.enable_slack_ingress && var.deploy_slack_ingress ? [1] : []
      content {
        paths   = ["/v1/provider/slack/events"]
        service = google_compute_backend_service.slack_ingress[0].id
      }
    }
  }
}

resource "google_compute_target_https_proxy" "edge" {
  project          = local.foundation.project_id
  name             = "${local.prefix}-control-plane-edge"
  url_map          = google_compute_url_map.https.id
  ssl_certificates = [google_compute_managed_ssl_certificate.edge.id]
  ssl_policy       = google_compute_ssl_policy.edge.id
}

resource "google_compute_global_forwarding_rule" "https" {
  project               = local.foundation.project_id
  name                  = "${local.prefix}-control-plane-https"
  ip_address            = google_compute_global_address.edge.id
  port_range            = "443"
  target                = google_compute_target_https_proxy.edge.id
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

resource "google_compute_url_map" "http_redirect" {
  project = local.foundation.project_id
  name    = "${local.prefix}-control-plane-http-redirect"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = true
  }
}

resource "google_compute_target_http_proxy" "redirect" {
  project = local.foundation.project_id
  name    = "${local.prefix}-control-plane-http-redirect"
  url_map = google_compute_url_map.http_redirect.id
}

resource "google_compute_global_forwarding_rule" "http" {
  project               = local.foundation.project_id
  name                  = "${local.prefix}-control-plane-http"
  ip_address            = google_compute_global_address.edge.id
  port_range            = "80"
  target                = google_compute_target_http_proxy.redirect.id
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

# --- SLO-grade observability (Phase 6 "hosted operations", hosted review
# gap #8: only seven job-failure alert policies existed; no latency/
# error-rate visibility, no dashboards, no per-service health signal).
#
# The control plane's Flask app emits one content-free "http_request" JSON
# line per request (attune.hosted.service_metrics.instrument_service_metrics)
# with only fixed-vocabulary fields -- service/route/method/status_class/
# status/duration_ms, never tenant, principal, query string, or any other
# identifier. See docs/decisions.md's 2026-07-19 SLO-monitoring entry for
# the field contract, and deploy/gcp/runtime/main.tf for the same pattern
# applied to the five private runtime services plus the worker's
# task_execution metric.
#
# Unconditional infrastructure, exactly like the existing
# export_download_failure policy above: control_plane is always deployed,
# so there is no separate service-activation gate to tie this to, and
# monitoring is not itself a customer-facing feature needing its own
# security-review gate.

resource "google_logging_metric" "control_plane_http_request_count" {
  project = local.foundation.project_id
  name    = "${local.prefix}-control-plane-http-request-count"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.control_plane.name}\"",
    "jsonPayload.metric=\"http_request\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Attune control-plane HTTP request count"

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

resource "google_logging_metric" "control_plane_http_request_latency" {
  project = local.foundation.project_id
  name    = "${local.prefix}-control-plane-http-request-latency"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.control_plane.name}\"",
    "jsonPayload.metric=\"http_request\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "ms"
    display_name = "Attune control-plane HTTP request latency"

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

resource "google_monitoring_alert_policy" "control_plane_5xx_error_rate" {
  project      = local.foundation.project_id
  display_name = "${local.prefix} control-plane 5xx error rate"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = <<-EOT
      The control plane returned more than ${var.slo_5xx_error_threshold}
      5xx responses within ${var.slo_alert_window_seconds} seconds.
      Inspect recent deploys and dependency health (identity, secret
      broker, dispatch broker, audit writer) before assuming a code
      regression. The signal contains no tenant or session data -- only
      the fixed route, method, and status class.
    EOT
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "Control-plane 5xx responses over threshold"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"logging.googleapis.com/user/${google_logging_metric.control_plane_http_request_count.name}\"",
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
  user_labels           = local.labels
}

resource "google_monitoring_alert_policy" "control_plane_p95_latency" {
  project      = local.foundation.project_id
  display_name = "${local.prefix} control-plane p95 latency"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = <<-EOT
      p95 control-plane request latency exceeded
      ${var.slo_control_plane_p95_latency_ms}ms over
      ${var.slo_alert_window_seconds} seconds. Inspect Cloud SQL, the
      secret broker, and the dispatch broker before raising the threshold.
    EOT
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "Control-plane p95 latency over threshold"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"logging.googleapis.com/user/${google_logging_metric.control_plane_http_request_latency.name}\"",
        "resource.type=\"cloud_run_revision\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = var.slo_control_plane_p95_latency_ms
      duration        = "0s"

      aggregations {
        alignment_period   = "${var.slo_alert_window_seconds}s"
        per_series_aligner = "ALIGN_PERCENTILE_95"
      }
    }
  }

  notification_channels = var.alert_notification_channels
  user_labels           = local.labels
}

# --- SLO dashboard.
#
# Built entirely from the log-based metrics above and in
# deploy/gcp/runtime/main.tf. Five of the six services' metrics are
# resources in the separate runtime Terraform root/state, not this one, so
# their metric type strings below are built from the same deterministic
# "${local.prefix}-<service>-..." naming both roots use -- not a direct
# Terraform resource reference. Keep the two naming schemes in sync if
# either changes; a mismatch just means an empty panel, never an error
# (Cloud Monitoring renders a chart with no data for an unknown metric
# type rather than failing).
locals {
  dashboard_services = [
    { key = "control-plane", label = "Control plane" },
    { key = "worker", label = "Worker" },
    { key = "model-gateway", label = "Model gateway" },
    { key = "dispatch-broker", label = "Dispatch broker" },
    { key = "secret-broker", label = "Secret broker" },
    { key = "channel-broker", label = "Channel broker" },
  ]

  dashboard_request_rate_widgets = [
    for service in local.dashboard_services : {
      title = "${service.label}: request rate"
      xyChart = {
        dataSets = [{
          timeSeriesQuery = {
            timeSeriesFilter = {
              filter = "metric.type=\"logging.googleapis.com/user/${local.prefix}-${service.key}-http-request-count\" AND resource.type=\"cloud_run_revision\""
              aggregation = {
                alignmentPeriod    = "60s"
                perSeriesAligner   = "ALIGN_RATE"
                crossSeriesReducer = "REDUCE_SUM"
              }
            }
          }
          plotType = "LINE"
        }]
        yAxis = { label = "requests/s", scale = "LINEAR" }
      }
    }
  ]

  dashboard_error_rate_widgets = [
    for service in local.dashboard_services : {
      title = "${service.label}: 5xx rate"
      xyChart = {
        dataSets = [{
          timeSeriesQuery = {
            timeSeriesFilter = {
              filter = "metric.type=\"logging.googleapis.com/user/${local.prefix}-${service.key}-http-request-count\" AND resource.type=\"cloud_run_revision\" AND metric.labels.status_class=\"5xx\""
              aggregation = {
                alignmentPeriod    = "60s"
                perSeriesAligner   = "ALIGN_RATE"
                crossSeriesReducer = "REDUCE_SUM"
              }
            }
          }
          plotType = "LINE"
        }]
        yAxis = { label = "5xx/s", scale = "LINEAR" }
      }
    }
  ]

  dashboard_latency_widgets = [
    for service in local.dashboard_services : {
      title = "${service.label}: p95 latency"
      xyChart = {
        dataSets = [{
          timeSeriesQuery = {
            timeSeriesFilter = {
              filter = "metric.type=\"logging.googleapis.com/user/${local.prefix}-${service.key}-http-request-latency\" AND resource.type=\"cloud_run_revision\""
              aggregation = {
                alignmentPeriod  = "60s"
                perSeriesAligner = "ALIGN_PERCENTILE_95"
              }
            }
          }
          plotType = "LINE"
        }]
        yAxis = { label = "ms", scale = "LINEAR" }
      }
    }
  ]

  dashboard_task_execution_widgets = [
    {
      title = "Worker: task-execution count by outcome"
      xyChart = {
        dataSets = [{
          timeSeriesQuery = {
            timeSeriesFilter = {
              filter = "metric.type=\"logging.googleapis.com/user/${local.prefix}-worker-task-execution-count\" AND resource.type=\"cloud_run_revision\""
              aggregation = {
                alignmentPeriod    = "60s"
                perSeriesAligner   = "ALIGN_RATE"
                crossSeriesReducer = "REDUCE_SUM"
                groupByFields      = ["metric.labels.task", "metric.labels.outcome"]
              }
            }
          }
          plotType = "STACKED_BAR"
        }]
        yAxis = { label = "tasks/s", scale = "LINEAR" }
      }
    },
    {
      title = "Worker: task-execution p95 latency by task"
      xyChart = {
        dataSets = [{
          timeSeriesQuery = {
            timeSeriesFilter = {
              filter = "metric.type=\"logging.googleapis.com/user/${local.prefix}-worker-task-execution-latency\" AND resource.type=\"cloud_run_revision\""
              aggregation = {
                alignmentPeriod  = "60s"
                perSeriesAligner = "ALIGN_PERCENTILE_95"
                groupByFields    = ["metric.labels.task"]
              }
            }
          }
          plotType = "LINE"
        }]
        yAxis = { label = "ms", scale = "LINEAR" }
      }
    },
  ]

  dashboard_widgets = concat(
    local.dashboard_request_rate_widgets,
    local.dashboard_error_rate_widgets,
    local.dashboard_latency_widgets,
    local.dashboard_task_execution_widgets,
  )
}

resource "google_monitoring_dashboard" "slo_overview" {
  project = local.foundation.project_id
  dashboard_json = jsonencode({
    displayName = "${local.prefix} SLO overview"
    gridLayout = {
      columns = "3"
      widgets = local.dashboard_widgets
    }
  })
}
