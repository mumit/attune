output "edge" {
  description = "Locked development edge identifiers and required DNS record."
  value = {
    hostname                    = var.hostname
    ipv4_address                = google_compute_global_address.edge.address
    dns_record_type             = "A"
    control_plane               = google_cloud_run_v2_service.control_plane.name
    oauth_callback_service      = google_cloud_run_v2_service.oauth_callback.name
    oauth_request_log_exclusion = google_logging_project_exclusion.oauth_callback_requests.name
    certificate                 = google_compute_managed_ssl_certificate.edge.name
    health_url                  = "https://${var.hostname}/healthz"
    oauth_callback              = "https://${var.hostname}/oauth/google/callback"
    oauth_is_enabled            = var.enable_google_workspace_oauth
    identity_sign_in_enabled    = var.enable_identity_sign_in
    google_chat_ingress = var.deploy_google_chat_ingress ? {
      service  = google_cloud_run_v2_service.google_chat_ingress[0].name
      endpoint = "https://${var.hostname}/v1/provider/google-chat/events"
      routed   = var.enable_google_chat_ingress
    } : null
  }
}
