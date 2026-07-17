variable "state_bucket" {
  description = "Private GCS bucket containing the foundation remote state."
  type        = string
}

variable "foundation_state_prefix" {
  description = "GCS prefix of the foundation Terraform state."
  type        = string
  default     = "foundation"
}

variable "migrator_image" {
  description = "Artifact Registry migrator image pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.migrator_image))
    error_message = "migrator_image must be an immutable @sha256 Artifact Registry reference."
  }
}

variable "initial_tenant_slug" {
  description = "Non-sensitive slug for the single operator-provisioned initial tenant."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,62}$", var.initial_tenant_slug))
    error_message = "initial_tenant_slug must be a lowercase DNS-style slug."
  }
}

variable "labels" {
  description = "Additional non-sensitive resource labels."
  type        = map(string)
  default     = {}
}

variable "protocol_retention_batch_size" {
  description = "Maximum rows pruned from each expired protocol table per execution."
  type        = number
  default     = 500

  validation {
    condition     = floor(var.protocol_retention_batch_size) == var.protocol_retention_batch_size && var.protocol_retention_batch_size >= 1 && var.protocol_retention_batch_size <= 1000
    error_message = "protocol_retention_batch_size must be an integer between 1 and 1000."
  }
}

variable "protocol_retention_max_batches" {
  description = "Maximum function calls per retention execution."
  type        = number
  default     = 4

  validation {
    condition     = floor(var.protocol_retention_max_batches) == var.protocol_retention_max_batches && var.protocol_retention_max_batches >= 1 && var.protocol_retention_max_batches <= 10
    error_message = "protocol_retention_max_batches must be an integer between 1 and 10."
  }
}

variable "enable_protocol_retention_schedule" {
  description = "Enable the independently authenticated daily protocol-retention schedule after its paused-path ceremony passes."
  type        = bool
  default     = false
}

variable "protocol_retention_schedule" {
  description = "Unix-cron schedule for expired-protocol retention."
  type        = string
  default     = "17 3 * * *"

  validation {
    condition     = can(regex("^[0-9*/?,\\-]+ [0-9*/?,\\-]+ [0-9*/?,\\-]+ [0-9*/?,\\-]+ [0-9*/?,\\-]+$", var.protocol_retention_schedule))
    error_message = "protocol_retention_schedule must contain exactly five non-empty unix-cron fields."
  }
}

variable "protocol_retention_time_zone" {
  description = "IANA time-zone name used to interpret the retention schedule."
  type        = string
  default     = "Etc/UTC"

  validation {
    condition     = can(regex("^[A-Za-z_+-]+(?:/[A-Za-z0-9_+.-]+)*$", var.protocol_retention_time_zone))
    error_message = "protocol_retention_time_zone must be an IANA-style time-zone name such as Etc/UTC or America/Vancouver."
  }
}

variable "alert_notification_channels" {
  description = "Monitoring notification-channel resource names for retention alerts."
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

variable "export_cleanup_batch_size" {
  description = "Maximum abandoned export-attempt objects leased per cleanup batch."
  type        = number
  default     = 50
  validation {
    condition     = floor(var.export_cleanup_batch_size) == var.export_cleanup_batch_size && var.export_cleanup_batch_size >= 1 && var.export_cleanup_batch_size <= 100
    error_message = "export_cleanup_batch_size must be an integer between 1 and 100."
  }
}

variable "export_cleanup_max_batches" {
  description = "Maximum cleanup claim batches per manually invoked execution."
  type        = number
  default     = 4
  validation {
    condition     = floor(var.export_cleanup_max_batches) == var.export_cleanup_max_batches && var.export_cleanup_max_batches >= 1 && var.export_cleanup_max_batches <= 10
    error_message = "export_cleanup_max_batches must be an integer between 1 and 10."
  }
}

variable "enable_export_cleanup_schedule" {
  description = "Activate the verified ten-minute customer-export cleanup schedule."
  type        = bool
  default     = false
}
