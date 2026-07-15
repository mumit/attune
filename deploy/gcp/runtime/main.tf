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
  dispatch_broker_labels   = merge(local.common_labels, { component = "dispatch-broker" })
  secret_broker_labels     = merge(local.common_labels, { component = "secret-broker" })
  worker_labels            = merge(local.common_labels, { component = "worker" })
  dispatch_broker_audience = "https://${local.prefix}-dispatch-broker.attune.internal"
  secret_broker_audience   = "https://${local.prefix}-secret-broker.attune.internal"
  worker_audience          = "https://${local.prefix}-worker.attune.internal"
  audit_callers = toset([
    local.foundation.workload_identities.control_plane,
    local.foundation.workload_identities.dispatch_broker,
    local.foundation.workload_identities.secret_broker,
    local.foundation.workload_identities.worker,
  ])
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
      min_instance_count = 0
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
  }
}

resource "google_cloud_run_v2_service_iam_member" "worker_invoker" {
  project  = local.foundation.project_id
  location = local.foundation.region
  name     = google_cloud_run_v2_service.worker.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.foundation.workload_identities.task_dispatch}"
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
        value = jsonencode([
          {
            purpose    = "platform.smoke"
            queue      = local.foundation.jobs_queue
            target_url = "${google_cloud_run_v2_service.worker.uri}/v1/tasks/dispatch"
            audience   = local.worker_audience
          }
        ])
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
      min_instance_count = 0
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
