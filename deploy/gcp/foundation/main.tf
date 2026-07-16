locals {
  prefix = "attune-${var.environment}"
  labels = merge(
    {
      application = "attune"
      environment = var.environment
      managed_by  = "terraform"
    },
    var.labels,
  )

  services = toset([
    "artifactregistry.googleapis.com",
    "billingbudgets.googleapis.com",
    "calendar-json.googleapis.com",
    "chat.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudkms.googleapis.com",
    "cloudtasks.googleapis.com",
    "compute.googleapis.com",
    "dns.googleapis.com",
    "gmail.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "identitytoolkit.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "orgpolicy.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
  ])

  platform_secret_ids = toset([
    "channel-reference-hmac",
    "google-oauth-client",
    "identity-bootstrap",
    "llm-api-key",
    "slack-client",
    "slack-signing-secret",
  ])

  fixed_google_api_hosts = {
    chat          = "chat.googleapis.com"
    oauth2        = "oauth2.googleapis.com"
    oauth-certs   = "www.googleapis.com"
    gmail         = "gmail.googleapis.com"
    secretmanager = "secretmanager.googleapis.com"
  }
}

data "google_project" "current" {
  project_id = var.project_id
}

resource "google_project_service" "required" {
  for_each           = local.services
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "private" {
  name                    = "${local.prefix}-private"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [google_project_service.required]
}

resource "google_compute_subnetwork" "application" {
  name                     = "${local.prefix}-application"
  region                   = var.region
  network                  = google_compute_network.private.id
  ip_cidr_range            = "10.42.0.0/20"
  private_ip_google_access = true

  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# Resolve only reviewed provider and platform hosts through Google's private API
# VIP. Other arbitrary internet destinations remain unreachable without NAT,
# and other googleapis.com names retain their existing resolution behavior.
resource "google_dns_managed_zone" "fixed_google_api" {
  for_each = local.fixed_google_api_hosts

  project     = var.project_id
  name        = "${local.prefix}-${each.key}-private-api"
  dns_name    = "${each.value}."
  description = "Exact private DNS boundary for ${each.value}"
  visibility  = "private"
  labels      = local.labels

  private_visibility_config {
    networks {
      network_url = google_compute_network.private.id
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_dns_record_set" "fixed_google_api" {
  for_each = local.fixed_google_api_hosts

  project      = var.project_id
  managed_zone = google_dns_managed_zone.fixed_google_api[each.key].name
  name         = "${each.value}."
  type         = "A"
  ttl          = 300
  rrdatas = [
    "199.36.153.8",
    "199.36.153.9",
    "199.36.153.10",
    "199.36.153.11",
  ]
}

resource "google_compute_global_address" "service_range" {
  name          = "${local.prefix}-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = google_compute_network.private.id
}

resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.private.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.service_range.name]

  depends_on = [google_project_service.required]
}

resource "google_kms_key_ring" "attune" {
  name     = local.prefix
  location = var.region

  depends_on = [google_project_service.required]
}

resource "google_kms_crypto_key" "database" {
  name            = "database"
  key_ring        = google_kms_key_ring.attune.id
  rotation_period = "7776000s"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key" "secrets" {
  name            = "secrets"
  key_ring        = google_kms_key_ring.attune.id
  rotation_period = "7776000s"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key" "connector_credentials" {
  name            = "connector-credentials"
  key_ring        = google_kms_key_ring.attune.id
  rotation_period = "7776000s"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key" "audit" {
  name            = "audit"
  key_ring        = google_kms_key_ring.attune.id
  rotation_period = "7776000s"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_service_identity" "cloud_sql" {
  provider = google-beta
  project  = var.project_id
  service  = "sqladmin.googleapis.com"

  depends_on = [google_project_service.required]
}

resource "google_project_service_identity" "secret_manager" {
  provider = google-beta
  project  = var.project_id
  service  = "secretmanager.googleapis.com"

  depends_on = [google_project_service.required]
}

resource "google_kms_crypto_key_iam_member" "cloud_sql" {
  crypto_key_id = google_kms_crypto_key.database.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.cloud_sql.email}"
}

resource "google_kms_crypto_key_iam_member" "secret_manager" {
  crypto_key_id = google_kms_crypto_key.secrets.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.secret_manager.email}"
}

resource "google_sql_database_instance" "postgres" {
  name                = "${local.prefix}-postgres"
  region              = var.region
  database_version    = var.database_version
  encryption_key_name = google_kms_crypto_key.database.id
  deletion_protection = true

  settings {
    tier                        = var.sql_tier
    edition                     = "ENTERPRISE"
    availability_type           = var.environment == "production" ? "REGIONAL" : "ZONAL"
    disk_type                   = "PD_SSD"
    disk_autoresize             = true
    deletion_protection_enabled = true
    user_labels                 = local.labels

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.private.id
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true

      backup_retention_settings {
        retained_backups = var.backup_retention_count
        retention_unit   = "COUNT"
      }
    }

    insights_config {
      query_insights_enabled  = true
      query_string_length     = 1024
      record_application_tags = true
      record_client_address   = false
    }
  }

  depends_on = [
    google_kms_crypto_key_iam_member.cloud_sql,
    google_service_networking_connection.private_services,
  ]
}

resource "google_sql_database" "attune" {
  name     = "attune"
  instance = google_sql_database_instance.postgres.name
}

resource "google_cloud_tasks_queue" "ingress" {
  name     = "${local.prefix}-ingress"
  location = var.region

  rate_limits {
    max_concurrent_dispatches = 25
    max_dispatches_per_second = 50
  }

  retry_config {
    max_attempts       = 20
    max_retry_duration = "86400s"
    min_backoff        = "1s"
    max_backoff        = "3600s"
    max_doublings      = 8
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_tasks_queue" "jobs" {
  name     = "${local.prefix}-jobs"
  location = var.region

  rate_limits {
    max_concurrent_dispatches = 50
    max_dispatches_per_second = 100
  }

  retry_config {
    max_attempts       = 20
    max_retry_duration = "604800s"
    min_backoff        = "2s"
    max_backoff        = "3600s"
    max_doublings      = 8
  }

  dynamic "http_target" {
    for_each = (
      var.jobs_worker_target_host == null &&
      var.jobs_worker_oidc_audience == null
      ? []
      : [true]
    )
    content {
      http_method = "POST"

      uri_override {
        host                      = var.jobs_worker_target_host
        scheme                    = "HTTPS"
        uri_override_enforce_mode = "ALWAYS"

        path_override {
          path = "/v1/tasks/dispatch"
        }
      }

      oidc_token {
        service_account_email = google_service_account.workload["task_dispatch"].email
        audience              = var.jobs_worker_oidc_audience
      }

      header_overrides {
        header {
          key   = "Content-Type"
          value = "application/json"
        }
      }
    }
  }

  depends_on = [google_project_service.required]

  lifecycle {
    precondition {
      condition = (
        (var.jobs_worker_target_host == null) ==
        (var.jobs_worker_oidc_audience == null)
      )
      error_message = "jobs worker host and OIDC audience must be configured together."
    }
  }
}

resource "google_pubsub_topic" "provider_events" {
  name                       = "${local.prefix}-provider-events"
  message_retention_duration = "86400s"
  labels                     = local.labels

  depends_on = [google_project_service.required]
}

resource "google_pubsub_topic_iam_member" "gmail_publish" {
  topic  = google_pubsub_topic.provider_events.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:gmail-api-push@system.gserviceaccount.com"
}

resource "google_artifact_registry_repository" "containers" {
  location      = var.region
  repository_id = local.prefix
  format        = "DOCKER"
  description   = "Signed Attune hosted-service images"
  labels        = local.labels

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "platform" {
  for_each  = local.platform_secret_ids
  secret_id = "${local.prefix}-${each.value}"
  labels    = local.labels

  replication {
    user_managed {
      replicas {
        location = var.region

        customer_managed_encryption {
          kms_key_name = google_kms_crypto_key.secrets.id
        }
      }
    }
  }

  depends_on = [google_kms_crypto_key_iam_member.secret_manager]
}

resource "google_kms_crypto_key_iam_member" "storage" {
  crypto_key_id = google_kms_crypto_key.audit.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.current.number}@gs-project-accounts.iam.gserviceaccount.com"
}

resource "google_storage_bucket" "audit" {
  name                        = "${var.project_id}-${local.prefix}-audit"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = local.labels

  encryption {
    default_kms_key_name = google_kms_crypto_key.audit.id
  }

  versioning {
    enabled = true
  }

  retention_policy {
    retention_period = var.audit_retention_days * 86400
    is_locked        = var.lock_audit_retention
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = var.environment != "production" || var.lock_audit_retention
      error_message = "Production requires lock_audit_retention=true; locking is permanent and must be reviewed before apply."
    }
  }

  depends_on = [google_kms_crypto_key_iam_member.storage]
}

resource "google_logging_project_sink" "retained_audit" {
  name                   = "${local.prefix}-retained-audit"
  destination            = "storage.googleapis.com/${google_storage_bucket.audit.name}"
  unique_writer_identity = true
  # Retain administrative Cloud Audit records, not application/request logs.
  # OAuth callbacks necessarily carry short-lived codes in their query string;
  # exporting all project logs would copy those codes into immutable storage.
  filter = join(" OR ", [
    "log_id(\"cloudaudit.googleapis.com/activity\")",
    "log_id(\"cloudaudit.googleapis.com/data_access\")",
    "log_id(\"cloudaudit.googleapis.com/policy\")",
    "log_id(\"cloudaudit.googleapis.com/system_event\")",
  ])

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "log_sink_create" {
  bucket = google_storage_bucket.audit.name
  role   = "roles/storage.objectCreator"
  member = google_logging_project_sink.retained_audit.writer_identity
}

resource "google_project_iam_audit_config" "all_services" {
  project = var.project_id
  service = "allServices"

  audit_log_config {
    log_type = "ADMIN_READ"
  }

  audit_log_config {
    log_type = "DATA_READ"
  }

  audit_log_config {
    log_type = "DATA_WRITE"
  }
}
