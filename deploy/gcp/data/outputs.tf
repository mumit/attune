output "migration_job" {
  description = "Operator-executed migration job identifiers."
  value = {
    project         = local.foundation.project_id
    region          = local.foundation.region
    name            = google_cloud_run_v2_job.migrate.name
    service_account = google_service_account.migrator.email
    image           = var.migrator_image
  }
}

output "identity_provisioning_job" {
  description = "Operator-executed initial identity provisioning job identifiers."
  value = {
    project          = local.foundation.project_id
    region           = local.foundation.region
    name             = google_cloud_run_v2_job.identity_provision.name
    service_account  = local.foundation.workload_identities.identity_provisioner
    image            = var.migrator_image
    bootstrap_secret = local.foundation.platform_secret_ids["identity-bootstrap"]
  }
}

output "protocol_retention_job" {
  description = "Bounded expired-protocol retention job and independent scheduler identifiers."
  value = {
    project                   = local.foundation.project_id
    region                    = local.foundation.region
    name                      = google_cloud_run_v2_job.protocol_retention.name
    service_account           = local.foundation.workload_identities.retention
    image                     = var.migrator_image
    scheduler_name            = google_cloud_scheduler_job.protocol_retention.name
    scheduler_service_account = local.foundation.workload_identities.retention_scheduler
    scheduler_paused          = google_cloud_scheduler_job.protocol_retention.paused
  }
}
