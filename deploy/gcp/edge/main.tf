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
        value = tostring(local.runtime.google_gmail_profile_enabled)
      }
      dynamic "env" {
        for_each = local.runtime.google_gmail_profile_enabled ? [1] : []
        content {
          name  = "ATTUNE_DISPATCH_BROKER_URL"
          value = local.runtime.dispatch_broker.uri
        }
      }
      dynamic "env" {
        for_each = local.runtime.google_gmail_profile_enabled ? [1] : []
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
      condition = !local.runtime.google_gmail_profile_enabled || (
        var.enable_google_workspace_oauth && local.runtime.dispatch_broker != null
      )
      error_message = "The browser connection test requires active Workspace OAuth and the fixed dispatch broker."
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

resource "google_compute_region_network_endpoint_group" "control_plane" {
  project               = local.foundation.project_id
  name                  = "${local.prefix}-control-plane"
  region                = local.foundation.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.control_plane.name
  }
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
    for_each = local.runtime.google_gmail_profile_enabled ? [1] : []
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
    for_each = local.runtime.google_gmail_profile_enabled ? [1] : []
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
          expression = "request.headers['host'] == '${var.hostname}' && (request.path == '/v1/identity/config' || request.path == '/assets/attune.css' || request.path == '/assets/identity.js')"
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
