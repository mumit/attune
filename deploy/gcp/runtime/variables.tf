variable "state_bucket" {
  description = "Private GCS bucket containing the foundation remote state."
  type        = string
}

variable "foundation_state_prefix" {
  description = "GCS prefix of the foundation Terraform state."
  type        = string
  default     = "foundation"
}

variable "audit_writer_image" {
  description = "Artifact Registry audit-writer image pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.audit_writer_image))
    error_message = "audit_writer_image must be an immutable @sha256 Artifact Registry reference."
  }
}

variable "dispatch_broker_image" {
  description = "Artifact Registry dispatch-broker image pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.dispatch_broker_image))
    error_message = "dispatch_broker_image must be an immutable @sha256 Artifact Registry reference."
  }
}

variable "channel_broker_image" {
  description = "Artifact Registry channel-broker image pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.channel_broker_image))
    error_message = "channel_broker_image must be an immutable @sha256 Artifact Registry reference."
  }
}

variable "enable_channel_broker" {
  description = "Deploy the private channel broker after migration 0022 and its security gates pass."
  type        = bool
  default     = false
}

variable "enable_dispatch_broker" {
  description = "Deploy dispatch only after the jobs queue fixed override is applied."
  type        = bool
  default     = false
}

variable "secret_broker_image" {
  description = "Artifact Registry secret-broker image pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.secret_broker_image))
    error_message = "secret_broker_image must be an immutable @sha256 Artifact Registry reference."
  }
}

variable "oauth_exchange_image" {
  description = "Artifact Registry OAuth-exchange image pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.oauth_exchange_image))
    error_message = "oauth_exchange_image must be an immutable @sha256 Artifact Registry reference."
  }
}

variable "oauth_min_instance_count" {
  description = "Warm-instance floor for the synchronous audit, secret-broker, and OAuth-exchange chain after connector activation."
  type        = number
  default     = 0

  validation {
    condition     = contains([0, 1], var.oauth_min_instance_count)
    error_message = "oauth_min_instance_count must be 0 while dormant or 1 after OAuth activation."
  }
}

variable "worker_image" {
  description = "Artifact Registry worker image pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.worker_image))
    error_message = "worker_image must be an immutable @sha256 Artifact Registry reference."
  }
}

variable "enable_google_gmail_profile" {
  description = "Register the fixed Gmail profile worker route after its security gates pass."
  type        = bool
  default     = false
}

variable "enable_google_workspace_verification" {
  description = "Register the composite fixed Gmail and Calendar connection-verification route after its security gates pass."
  type        = bool
  default     = false
}

variable "alert_notification_channels" {
  description = "Monitoring notification-channel resource names for runtime security alerts."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for channel in var.alert_notification_channels :
      can(regex("^projects/[^/]+/notificationChannels/[0-9]+$", channel))
    ])
    error_message = "alert_notification_channels entries must be full Monitoring notification-channel resource names."
  }
}

variable "labels" {
  description = "Additional non-sensitive resource labels."
  type        = map(string)
  default     = {}
}
