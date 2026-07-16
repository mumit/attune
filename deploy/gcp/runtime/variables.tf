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

variable "model_gateway_image" {
  description = "Artifact Registry model-gateway image pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.model_gateway_image))
    error_message = "model_gateway_image must be an immutable @sha256 Artifact Registry reference."
  }
}

variable "enable_model_gateway" {
  description = "Deploy the private fixed-task model gateway; this alone does not activate conversation."
  type        = bool
  default     = false
}

variable "enable_google_chat_conversation" {
  description = "Register the bounded Google Chat conversation route and grant its worker/broker edges after all executors pass security review."
  type        = bool
  default     = false
}

variable "hosted_timezone" {
  description = "Operator-confirmed IANA timezone used to ground relative dates until per-principal timezone preferences are available."
  type        = string
  default     = "UTC"

  validation {
    condition = (
      var.hosted_timezone == "UTC" ||
      can(regex("^[A-Za-z][A-Za-z0-9_+-]{0,31}/[A-Za-z0-9_+./-]{1,127}$", var.hosted_timezone))
    )
    error_message = "hosted_timezone must be UTC or a bounded IANA timezone name such as America/Vancouver."
  }
}

variable "llm_base_url" {
  description = "Operator-fixed OpenAI-compatible HTTPS origin or base path used only by the model gateway."
  type        = string
  default     = "https://api.openai.com/v1"

  validation {
    condition = (
      startswith(var.llm_base_url, "https://") &&
      !strcontains(var.llm_base_url, "@") &&
      !strcontains(var.llm_base_url, "?") &&
      !strcontains(var.llm_base_url, "#") &&
      length(var.llm_base_url) <= 1024
    )
    error_message = "llm_base_url must be a bounded fixed HTTPS URL without credentials, query, or fragment."
  }
}

variable "model_classify" {
  description = "Operator-fixed low-latency model route for bounded classification."
  type        = string
  default     = "gpt-4.1-mini"

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$", var.model_classify))
    error_message = "model_classify must be a valid fixed model route."
  }
}

variable "model_converse" {
  description = "Operator-fixed model route for bounded assistant responses."
  type        = string
  default     = "gpt-4.1"

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$", var.model_converse))
    error_message = "model_converse must be a valid fixed model route."
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
