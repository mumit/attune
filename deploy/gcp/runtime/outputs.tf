output "audit_writer" {
  description = "Private audit-writer service identifiers."
  value = {
    project         = local.foundation.project_id
    region          = local.foundation.region
    name            = google_cloud_run_v2_service.audit_writer.name
    uri             = google_cloud_run_v2_service.audit_writer.uri
    service_account = local.foundation.workload_identities.audit_writer
    image           = var.audit_writer_image
  }
}

output "dispatch_broker" {
  description = "Private fixed-route dispatch-broker service identifiers."
  value = var.enable_dispatch_broker ? {
    project         = local.foundation.project_id
    region          = local.foundation.region
    name            = google_cloud_run_v2_service.dispatch_broker[0].name
    uri             = google_cloud_run_v2_service.dispatch_broker[0].uri
    audience        = local.dispatch_broker_audience
    service_account = local.foundation.workload_identities.dispatch_broker
    image           = var.dispatch_broker_image
  } : null
}

output "channel_broker" {
  description = "Private one-use channel-link broker service identifiers."
  value = var.enable_channel_broker ? {
    project         = local.foundation.project_id
    region          = local.foundation.region
    name            = google_cloud_run_v2_service.channel_broker[0].name
    uri             = google_cloud_run_v2_service.channel_broker[0].uri
    audience        = local.channel_broker_audience
    service_account = local.foundation.workload_identities.channel_broker
    image           = var.channel_broker_image
  } : null
}

output "secret_broker" {
  description = "Private secret-broker service identifiers."
  value = {
    project         = local.foundation.project_id
    region          = local.foundation.region
    name            = google_cloud_run_v2_service.secret_broker.name
    uri             = google_cloud_run_v2_service.secret_broker.uri
    audience        = local.secret_broker_audience
    service_account = local.foundation.workload_identities.secret_broker
    image           = var.secret_broker_image
  }
}

output "oauth_exchange" {
  description = "Private one-time OAuth exchange service identifiers."
  value = {
    project         = local.foundation.project_id
    region          = local.foundation.region
    name            = google_cloud_run_v2_service.oauth_exchange.name
    uri             = google_cloud_run_v2_service.oauth_exchange.uri
    audience        = local.oauth_exchange_audience
    service_account = local.foundation.workload_identities.oauth_exchange
    image           = var.oauth_exchange_image
  }
}

output "worker" {
  description = "Private deterministic worker service identifiers."
  value = {
    project         = local.foundation.project_id
    region          = local.foundation.region
    name            = google_cloud_run_v2_service.worker.name
    uri             = google_cloud_run_v2_service.worker.uri
    target_path     = "/v1/tasks/dispatch"
    audience        = local.worker_audience
    service_account = local.foundation.workload_identities.worker
    image           = var.worker_image
  }
}

output "google_gmail_profile_enabled" {
  description = "Whether the reviewed fixed Gmail profile route is active."
  value       = var.enable_google_gmail_profile
}

output "google_workspace_verification_enabled" {
  description = "Whether the reviewed composite Gmail and Calendar connection-verification route is active."
  value       = var.enable_google_workspace_verification
}
