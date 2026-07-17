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
  labels = merge(
    {
      application = "attune"
      environment = local.foundation.environment
      managed_by  = "terraform"
      component   = "database-migration"
    },
    var.labels,
  )
  runtime_database_users = {
    attune_channel_broker = trimsuffix(
      local.foundation.workload_identities.channel_broker,
      ".gserviceaccount.com",
    )
    attune_control_plane = trimsuffix(
      local.foundation.workload_identities.control_plane,
      ".gserviceaccount.com",
    )
    attune_worker = trimsuffix(
      local.foundation.workload_identities.worker,
      ".gserviceaccount.com",
    )
    attune_dispatch_broker = trimsuffix(
      local.foundation.workload_identities.dispatch_broker,
      ".gserviceaccount.com",
    )
    attune_export = trimsuffix(
      local.foundation.workload_identities.export,
      ".gserviceaccount.com",
    )
    attune_export_cleanup = trimsuffix(
      local.foundation.workload_identities.export_cleanup,
      ".gserviceaccount.com",
    )
    attune_export_download = trimsuffix(
      local.foundation.workload_identities.export_download,
      ".gserviceaccount.com",
    )
    attune_secret_broker = trimsuffix(
      local.foundation.workload_identities.secret_broker,
      ".gserviceaccount.com",
    )
    attune_audit_writer = trimsuffix(
      local.foundation.workload_identities.audit_writer,
      ".gserviceaccount.com",
    )
    attune_oauth_exchange = trimsuffix(
      local.foundation.workload_identities.oauth_exchange,
      ".gserviceaccount.com",
    )
    attune_identity_provisioner = trimsuffix(
      local.foundation.workload_identities.identity_provisioner,
      ".gserviceaccount.com",
    )
    attune_retention = trimsuffix(
      local.foundation.workload_identities.retention,
      ".gserviceaccount.com",
    )
  }
}

resource "google_service_account" "migrator" {
  project      = local.foundation.project_id
  account_id   = "${local.prefix}-migrate"
  display_name = "Attune ${local.foundation.environment} database migrator"
  description  = "Dedicated bulk-access identity used only by the reviewed migration job"
}

resource "google_project_iam_member" "migrator_cloud_sql_client" {
  project = local.foundation.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.migrator.email}"
}

resource "google_project_iam_member" "migrator_cloud_sql_login" {
  project = local.foundation.project_id
  role    = "roles/cloudsql.instanceUser"
  member  = "serviceAccount:${google_service_account.migrator.email}"
}

resource "google_project_iam_member" "migrator_logs" {
  project = local.foundation.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.migrator.email}"
}

resource "google_sql_user" "migrator" {
  project  = local.foundation.project_id
  instance = element(split(":", local.foundation.database_instance), 2)
  name = trimsuffix(
    google_service_account.migrator.email,
    ".gserviceaccount.com",
  )
  type           = "CLOUD_IAM_SERVICE_ACCOUNT"
  database_roles = ["cloudsqlsuperuser"]
}

resource "google_cloud_run_v2_job" "migrate" {
  project             = local.foundation.project_id
  name                = "${local.prefix}-database-migrate"
  location            = local.foundation.region
  deletion_protection = true
  labels              = local.labels

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.migrator.email
      max_retries     = 0
      timeout         = "900s"

      containers {
        name  = "migrator"
        image = var.migrator_image

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
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
            google_service_account.migrator.email,
            ".gserviceaccount.com",
          )
        }
        env {
          name  = "ATTUNE_DB_ROLE_BINDINGS"
          value = jsonencode(local.runtime_database_users)
        }
      }

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = local.foundation.network_id
          subnetwork = local.foundation.subnetwork_id
          tags       = ["attune-database-migration"]
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_iam_member.migrator_cloud_sql_client,
    google_project_iam_member.migrator_cloud_sql_login,
    google_sql_user.migrator,
  ]
}

resource "google_cloud_run_v2_job" "identity_provision" {
  project             = local.foundation.project_id
  name                = "${local.prefix}-identity-provision"
  location            = local.foundation.region
  deletion_protection = true
  labels = merge(local.labels, {
    component = "identity-provisioning"
  })

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = local.foundation.workload_identities.identity_provisioner
      max_retries     = 0
      timeout         = "300s"

      containers {
        name    = "identity-provisioner"
        image   = var.migrator_image
        command = ["python", "-m", "attune.hosted.provision_identity"]

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
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
            local.foundation.workload_identities.identity_provisioner,
            ".gserviceaccount.com",
          )
        }
        env {
          name  = "ATTUNE_IDENTITY_BOOTSTRAP_SECRET"
          value = local.foundation.platform_secret_ids["identity-bootstrap"]
        }
        env {
          name  = "ATTUNE_IDENTITY_ISSUER"
          value = "https://securetoken.google.com/${local.foundation.project_id}"
        }
        env {
          name  = "ATTUNE_INITIAL_TENANT_SLUG"
          value = var.initial_tenant_slug
        }
        env {
          name  = "ATTUNE_INITIAL_TENANT_REGION"
          value = local.foundation.region
        }
      }

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = local.foundation.network_id
          subnetwork = local.foundation.subnetwork_id
          tags       = ["attune-identity-provisioning"]
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_cloud_run_v2_job" "protocol_retention" {
  project             = local.foundation.project_id
  name                = "${local.prefix}-protocol-retention"
  location            = local.foundation.region
  deletion_protection = true
  labels = merge(local.labels, {
    component = "protocol-retention"
  })

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = local.foundation.workload_identities.retention
      max_retries     = 0
      timeout         = "300s"

      containers {
        name    = "protocol-retention"
        image   = var.migrator_image
        command = ["python", "-m", "attune.hosted.protocol_retention"]

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
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
            local.foundation.workload_identities.retention,
            ".gserviceaccount.com",
          )
        }
        env {
          name  = "ATTUNE_RETENTION_BATCH_SIZE"
          value = tostring(var.protocol_retention_batch_size)
        }
        env {
          name  = "ATTUNE_RETENTION_MAX_BATCHES"
          value = tostring(var.protocol_retention_max_batches)
        }
      }

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = local.foundation.network_id
          subnetwork = local.foundation.subnetwork_id
          tags       = ["attune-protocol-retention"]
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_cloud_run_v2_job" "export_cleanup" {
  project             = local.foundation.project_id
  name                = "${local.prefix}-export-cleanup"
  location            = local.foundation.region
  deletion_protection = true
  labels              = merge(local.labels, { component = "export-cleanup" })

  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = local.foundation.workload_identities.export_cleanup
      max_retries     = 0
      timeout         = "300s"
      containers {
        name    = "export-cleanup"
        image   = var.migrator_image
        command = ["python", "-m", "attune.hosted.export_cleanup"]
        resources { limits = { cpu = "1", memory = "512Mi" } }
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
          value = trimsuffix(local.foundation.workload_identities.export_cleanup, ".gserviceaccount.com")
        }
        env {
          name  = "ATTUNE_EXPORT_BUCKET"
          value = local.foundation.customer_export_bucket
        }
        env {
          name  = "ATTUNE_EXPORT_CLEANUP_BATCH_SIZE"
          value = tostring(var.export_cleanup_batch_size)
        }
        env {
          name  = "ATTUNE_EXPORT_CLEANUP_MAX_BATCHES"
          value = tostring(var.export_cleanup_max_batches)
        }
      }
      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = local.foundation.network_id
          subnetwork = local.foundation.subnetwork_id
          tags       = ["attune-export-cleanup"]
        }
      }
    }
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_cloud_run_v2_job_iam_member" "export_cleanup_scheduler" {
  project  = local.foundation.project_id
  location = google_cloud_run_v2_job.export_cleanup.location
  name     = google_cloud_run_v2_job.export_cleanup.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.export_cleanup_scheduler}"
}

resource "google_cloud_scheduler_job" "export_cleanup" {
  project          = local.foundation.project_id
  region           = local.foundation.region
  name             = "${local.prefix}-export-cleanup"
  description      = "Runs bounded exact-generation cleanup for abandoned, expired, and consumed customer exports."
  schedule         = "*/10 * * * *"
  time_zone        = "Etc/UTC"
  paused           = !var.enable_export_cleanup_schedule
  attempt_deadline = "300s"
  retry_config {
    retry_count          = 1
    max_retry_duration   = "600s"
    min_backoff_duration = "30s"
    max_backoff_duration = "60s"
    max_doublings        = 1
  }
  http_target {
    uri         = "https://run.googleapis.com/v2/projects/${local.foundation.project_id}/locations/${local.foundation.region}/jobs/${google_cloud_run_v2_job.export_cleanup.name}:run"
    http_method = "POST"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }
    oauth_token {
      service_account_email = local.foundation.workload_identities.export_cleanup_scheduler
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on      = [google_cloud_run_v2_job_iam_member.export_cleanup_scheduler]
  deletion_policy = "PREVENT"
}

# The scheduler can start only this job. It has no database, logging, metrics,
# or service-level runtime role; the job itself assumes the separate retention
# executor identity after Cloud Run accepts this authenticated control request.
resource "google_cloud_run_v2_job_iam_member" "protocol_retention_scheduler" {
  project  = local.foundation.project_id
  location = google_cloud_run_v2_job.protocol_retention.location
  name     = google_cloud_run_v2_job.protocol_retention.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.retention_scheduler}"
}

resource "google_cloud_scheduler_job" "protocol_retention" {
  project     = local.foundation.project_id
  region      = local.foundation.region
  name        = "${local.prefix}-protocol-retention"
  description = "Starts the bounded expired-protocol retention job; deploy paused and activate only after the scheduler-path ceremony."
  schedule    = var.protocol_retention_schedule
  time_zone   = var.protocol_retention_time_zone
  paused      = !var.enable_protocol_retention_schedule

  attempt_deadline = "300s"

  retry_config {
    retry_count          = 1
    max_retry_duration   = "600s"
    min_backoff_duration = "30s"
    max_backoff_duration = "60s"
    max_doublings        = 1
  }

  http_target {
    uri         = "https://run.googleapis.com/v2/projects/${local.foundation.project_id}/locations/${local.foundation.region}/jobs/${google_cloud_run_v2_job.protocol_retention.name}:run"
    http_method = "POST"
    body        = base64encode("{}")
    headers = {
      "Content-Type" = "application/json"
    }

    oauth_token {
      service_account_email = local.foundation.workload_identities.retention_scheduler
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.protocol_retention_scheduler]

  deletion_policy = "PREVENT"
}

resource "google_logging_metric" "protocol_retention_failure" {
  project = local.foundation.project_id
  name    = "${local.prefix}-protocol-retention-failure"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.job_name=\"${google_cloud_run_v2_job.protocol_retention.name}\"",
    "severity>=ERROR",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Attune protocol-retention failures"
  }
}

resource "google_logging_metric" "protocol_retention_backlog" {
  project = local.foundation.project_id
  name    = "${local.prefix}-protocol-retention-backlog"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.job_name=\"${google_cloud_run_v2_job.protocol_retention.name}\"",
    "jsonPayload.event=\"attune_protocol_retention\"",
    "jsonPayload.backlog_possible=true",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Attune protocol-retention possible backlog"
  }
}

resource "google_monitoring_alert_policy" "protocol_retention_failure" {
  project      = local.foundation.project_id
  display_name = "${local.prefix} protocol-retention failure"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = "The bounded protocol-retention job logged an error. Keep scheduling disabled or pause it, inspect the execution and database verifier, and do not broaden the retention identity."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "At least one retention error"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.protocol_retention_failure.name}\" AND resource.type=\"cloud_run_job\""
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

resource "google_monitoring_alert_policy" "protocol_retention_backlog" {
  project      = local.foundation.project_id
  display_name = "${local.prefix} protocol-retention possible backlog"
  combiner     = "OR"
  enabled      = true

  documentation {
    content   = "The bounded protocol-retention job saturated every configured batch and expired records may remain. Run it again after investigation; do not raise limits without storage and load review."
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "Retention batch ceiling reached"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.protocol_retention_backlog.name}\" AND resource.type=\"cloud_run_job\""
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

resource "google_logging_metric" "export_cleanup_failure" {
  project = local.foundation.project_id
  name    = "${local.prefix}-export-cleanup-failure"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.job_name=\"${google_cloud_run_v2_job.export_cleanup.name}\"",
    "severity>=ERROR",
  ])
  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Attune export-cleanup failures"
  }
}

resource "google_logging_metric" "export_cleanup_backlog" {
  project = local.foundation.project_id
  name    = "${local.prefix}-export-cleanup-backlog"
  filter = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.job_name=\"${google_cloud_run_v2_job.export_cleanup.name}\"",
    "jsonPayload.event=\"attune_export_cleanup\"",
    "jsonPayload.backlog_possible=true",
  ])
  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Attune export-cleanup possible backlog"
  }
}

resource "google_monitoring_alert_policy" "export_cleanup_failure" {
  project      = local.foundation.project_id
  display_name = "${local.prefix} export-cleanup failure"
  combiner     = "OR"
  enabled      = true
  documentation {
    content   = "Abandoned export-object cleanup failed. Inspect the exact execution; do not broaden storage authority or mark the attempt cleaned manually."
    mime_type = "text/markdown"
  }
  conditions {
    display_name = "At least one export cleanup error"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.export_cleanup_failure.name}\" AND resource.type=\"cloud_run_job\""
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

resource "google_monitoring_alert_policy" "export_cleanup_backlog" {
  project      = local.foundation.project_id
  display_name = "${local.prefix} export-cleanup possible backlog"
  combiner     = "OR"
  enabled      = true
  documentation {
    content   = "Export cleanup saturated every bounded batch. Re-run after investigation; do not raise limits without storage and database review."
    mime_type = "text/markdown"
  }
  conditions {
    display_name = "Export cleanup batch ceiling reached"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.export_cleanup_backlog.name}\" AND resource.type=\"cloud_run_job\""
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
