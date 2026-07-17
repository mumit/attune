locals {
  workload_accounts = {
    channel_broker           = "chan-broker"
    control_plane            = "ctl"
    oauth_callback           = "oauth-cb"
    oauth_exchange           = "oauth-xchg"
    dispatch_broker          = "task-broker"
    export                   = "export"
    export_cleanup           = "exp-clean"
    export_cleanup_scheduler = "exp-cln-sch"
    export_download          = "exp-down"
    ingress                  = "ingress"
    identity_provisioner     = "id-prov"
    model_gateway            = "model"
    retention                = "retention"
    retention_scheduler      = "ret-sched"
    worker                   = "worker"
    secret_broker            = "secrets"
    task_dispatch            = "dispatch"
    audit_writer             = "audit"
  }
}

resource "google_service_account" "workload" {
  for_each     = local.workload_accounts
  account_id   = "${local.prefix}-${each.value}"
  display_name = "Attune ${var.environment} ${replace(each.key, "_", " ")}"
  description  = "Dedicated identity for the Attune ${each.key} trust boundary"
}

resource "google_project_iam_member" "runtime_logging" {
  for_each = {
    for name, account in google_service_account.workload : name => account
    if !contains(["export", "export_cleanup_scheduler", "export_download", "oauth_callback", "oauth_exchange", "retention_scheduler"], name)
  }
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${each.value.email}"
}

resource "google_project_iam_member" "runtime_metrics" {
  for_each = {
    for name, account in google_service_account.workload : name => account
    if !contains(["export", "export_cleanup_scheduler", "export_download", "retention_scheduler"], name)
  }
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${each.value.email}"
}

resource "google_project_iam_member" "database_client" {
  for_each = toset([
    "audit_writer",
    "channel_broker",
    "control_plane",
    "dispatch_broker",
    "export",
    "export_cleanup",
    "export_download",
    "oauth_exchange",
    "identity_provisioner",
    "retention",
    "secret_broker",
    "worker",
  ])
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.workload[each.value].email}"
}

resource "google_project_iam_member" "database_instance_user" {
  for_each = toset([
    "audit_writer",
    "channel_broker",
    "control_plane",
    "dispatch_broker",
    "export",
    "export_cleanup",
    "export_download",
    "oauth_exchange",
    "identity_provisioner",
    "retention",
    "secret_broker",
    "worker",
  ])
  project = var.project_id
  role    = "roles/cloudsql.instanceUser"
  member  = "serviceAccount:${google_service_account.workload[each.value].email}"
}

resource "google_sql_user" "workload" {
  for_each = toset([
    "audit_writer",
    "channel_broker",
    "control_plane",
    "dispatch_broker",
    "export",
    "export_cleanup",
    "oauth_exchange",
    "identity_provisioner",
    "retention",
    "secret_broker",
    "worker",
  ])
  name = trimsuffix(
    google_service_account.workload[each.value].email,
    ".gserviceaccount.com",
  )
  instance = google_sql_database_instance.postgres.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

resource "google_cloud_tasks_queue_iam_member" "ingress_enqueuer" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_tasks_queue.ingress.name
  role     = "roles/cloudtasks.enqueuer"
  member   = "serviceAccount:${google_service_account.workload["dispatch_broker"].email}"
}

resource "google_cloud_tasks_queue_iam_member" "jobs_enqueuer" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_tasks_queue.jobs.name
  role     = "roles/cloudtasks.enqueuer"
  member   = "serviceAccount:${google_service_account.workload["dispatch_broker"].email}"
}

resource "google_service_account_iam_member" "task_identity_user" {
  service_account_id = google_service_account.workload["task_dispatch"].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.workload["dispatch_broker"].email}"
}

resource "google_project_service_identity" "cloud_tasks" {
  provider = google-beta
  project  = var.project_id
  service  = "cloudtasks.googleapis.com"

  depends_on = [google_project_service.required]
}

resource "google_service_account_iam_member" "cloud_tasks_token_creator" {
  service_account_id = google_service_account.workload["task_dispatch"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_project_service_identity.cloud_tasks.email}"
}

resource "google_secret_manager_secret_iam_member" "broker_access" {
  for_each = {
    for name, secret in google_secret_manager_secret.platform : name => secret
    if !contains(["channel-reference-hmac", "identity-bootstrap", "llm-api-key"], name)
  }
  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.workload["secret_broker"].email}"
}

resource "google_secret_manager_secret_iam_member" "model_gateway_llm_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.platform["llm-api-key"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.workload["model_gateway"].email}"
}

resource "google_secret_manager_secret_iam_member" "channel_broker_hmac_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.platform["channel-reference-hmac"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.workload["channel_broker"].email}"
}

resource "google_secret_manager_secret_iam_member" "identity_bootstrap_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.platform["identity-bootstrap"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.workload["identity_provisioner"].email}"
}

resource "google_kms_crypto_key_iam_member" "broker_connector_crypto" {
  crypto_key_id = google_kms_crypto_key.connector_credentials.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_service_account.workload["secret_broker"].email}"
}

resource "google_kms_crypto_key_iam_member" "channel_broker_route_crypto" {
  crypto_key_id = google_kms_crypto_key.connector_credentials.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_service_account.workload["channel_broker"].email}"
}

resource "google_kms_crypto_key_iam_member" "export_wrap" {
  crypto_key_id = google_kms_crypto_key.customer_export.id
  role          = "roles/cloudkms.cryptoKeyEncrypter"
  member        = "serviceAccount:${google_service_account.workload["export"].email}"
}

resource "google_kms_crypto_key_iam_member" "export_download_unwrap" {
  crypto_key_id = google_kms_crypto_key.customer_export.id
  role          = "roles/cloudkms.cryptoKeyDecrypter"
  member        = "serviceAccount:${google_service_account.workload["export_download"].email}"
}

resource "google_project_iam_custom_role" "export_object_writer" {
  project     = var.project_id
  role_id     = "attune_${var.environment}_export_writer"
  title       = "Attune ${var.environment} export object writer"
  description = "Create and delete opaque temporary export objects without reading or listing them."
  permissions = [
    "storage.objects.create",
    "storage.objects.delete",
  ]
  stage = "GA"
}

resource "google_project_iam_custom_role" "export_bucket_policy_admin" {
  project     = var.project_id
  role_id     = "attune_${var.environment}_export_policy_admin"
  title       = "Attune ${var.environment} export bucket policy administrator"
  description = "Manage temporary export bucket policy without access to export objects."
  permissions = [
    "storage.buckets.get",
    "storage.buckets.getIamPolicy",
    "storage.buckets.setIamPolicy",
  ]
  stage = "GA"
}

resource "google_project_iam_custom_role" "export_object_cleanup" {
  project     = var.project_id
  role_id     = "attune_${var.environment}_export_cleanup"
  title       = "Attune ${var.environment} export object cleanup"
  description = "Delete known opaque temporary export objects without creating, reading, or listing them."
  permissions = ["storage.objects.delete"]
  stage       = "GA"
}

resource "google_project_iam_custom_role" "export_object_reader" {
  project     = var.project_id
  role_id     = "attune_${var.environment}_export_reader"
  title       = "Attune ${var.environment} export object reader"
  description = "Read only an application-authorized temporary export object without listing, creating, or deleting."
  permissions = ["storage.objects.get"]
  stage       = "GA"
}

resource "google_project_iam_member" "export_bucket_policy_admin" {
  for_each = var.export_bucket_policy_admin_members
  project  = var.project_id
  role     = google_project_iam_custom_role.export_bucket_policy_admin.name
  member   = each.value
}

data "google_iam_policy" "customer_export" {
  binding {
    role = google_project_iam_custom_role.export_object_writer.name
    members = [
      "serviceAccount:${google_service_account.workload["export"].email}",
    ]
  }
  binding {
    role = google_project_iam_custom_role.export_object_cleanup.name
    members = [
      "serviceAccount:${google_service_account.workload["export_cleanup"].email}",
    ]
  }
  binding {
    role = google_project_iam_custom_role.export_object_reader.name
    members = [
      "serviceAccount:${google_service_account.workload["export_download"].email}",
    ]
  }
}

resource "google_storage_bucket_iam_policy" "customer_export" {
  bucket      = google_storage_bucket.customer_export.name
  policy_data = data.google_iam_policy.customer_export.policy_data

  depends_on = [google_project_iam_member.export_bucket_policy_admin]
}

resource "google_storage_bucket_iam_member" "audit_create" {
  bucket = google_storage_bucket.audit.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.workload["audit_writer"].email}"
}
