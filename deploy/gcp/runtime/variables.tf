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

variable "export_writer_image" {
  description = "Artifact Registry customer-export writer image pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.export_writer_image))
    error_message = "export_writer_image must be an immutable @sha256 Artifact Registry reference."
  }
}

variable "enable_export_writer" {
  description = "Deploy and register the private customer-export writer after cleanup and database gates pass."
  type        = bool
  default     = false
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

variable "enable_slack_conversation" {
  description = "Register the bounded Slack conversation route and grant its worker/broker edges after all executors pass security review."
  type        = bool
  default     = false
}

variable "enable_web_conversation" {
  description = "Register the bounded hosted web conversation worker route and its model-gateway edge after security review; this route never touches the channel broker."
  type        = bool
  default     = false
}

variable "slack_channel_enabled" {
  description = "Configure the private broker's Slack installation routes after the platform Slack app, its secret versions, and their security gates exist."
  type        = bool
  default     = false
}

variable "slack_client_id" {
  description = "Public client ID of the platform-owned Slack app; required only when the Slack channel is enabled."
  type        = string
  default     = ""

  validation {
    condition     = var.slack_client_id == "" || can(regex("^[0-9]{6,20}\\.[0-9]{6,20}$", var.slack_client_id))
    error_message = "slack_client_id must be empty or a syntactically valid Slack app client ID."
  }
}

variable "slack_app_id" {
  description = "Public app identifier of the platform-owned Slack app; required only when the Slack channel is enabled."
  type        = string
  default     = ""

  validation {
    condition     = var.slack_app_id == "" || can(regex("^A[A-Z0-9]{5,20}$", var.slack_app_id))
    error_message = "slack_app_id must be empty or a syntactically valid Slack app ID."
  }
}

variable "slack_redirect_uri" {
  description = "Exact HTTPS OAuth redirect URI of the platform-owned Slack app; required only when the Slack channel is enabled."
  type        = string
  default     = ""

  validation {
    condition = var.slack_redirect_uri == "" || (
      startswith(var.slack_redirect_uri, "https://") &&
      !strcontains(var.slack_redirect_uri, "@") &&
      !strcontains(var.slack_redirect_uri, "#") &&
      length(var.slack_redirect_uri) <= 1024
    )
    error_message = "slack_redirect_uri must be empty or a bounded fixed HTTPS URL without credentials or fragment."
  }
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

variable "model_embed" {
  description = "Operator-fixed model route for bounded embeddings. model_gateway_app.py reads ATTUNE_MODEL_EMBED unconditionally (it is part of the fixed standard_models map alongside classify/converse); previously unwired here, which would have crashed the gateway on first boot."
  type        = string
  default     = "text-embedding-3-small"

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$", var.model_embed))
    error_message = "model_embed must be a valid fixed model route."
  }
}

variable "model_premium_classify" {
  description = "Operator-fixed premium classification model route; required only when enable_tenant_model_profiles is true (ATTUNE_MODEL_PREMIUM_CLASSIFY)."
  type        = string
  default     = ""

  validation {
    condition     = var.model_premium_classify == "" || can(regex("^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$", var.model_premium_classify))
    error_message = "model_premium_classify must be empty or a valid fixed model route."
  }
}

variable "model_premium_converse" {
  description = "Operator-fixed premium conversation model route; required only when enable_tenant_model_profiles is true (ATTUNE_MODEL_PREMIUM_CONVERSE)."
  type        = string
  default     = ""

  validation {
    condition     = var.model_premium_converse == "" || can(regex("^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$", var.model_premium_converse))
    error_message = "model_premium_converse must be empty or a valid fixed model route."
  }
}

variable "model_premium_embed" {
  description = "Operator-fixed premium embedding model route; required only when enable_tenant_model_profiles is true (ATTUNE_MODEL_PREMIUM_EMBED)."
  type        = string
  default     = ""

  validation {
    condition     = var.model_premium_embed == "" || can(regex("^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$", var.model_premium_embed))
    error_message = "model_premium_embed must be empty or a valid fixed model route."
  }
}

variable "enable_google_gmail_profile" {
  description = "Register the fixed Gmail profile worker route after its security gates pass."
  type        = bool
  default     = false
}

variable "enable_hosted_memory" {
  description = "Register the worker's dormant hosted conversational memory repository (ATTUNE_ENABLE_HOSTED_MEMORY). Implemented and tested but never deployed until an operator flips this in a reviewed plan (docs/hosted-memory.md)."
  type        = bool
  default     = false
}

variable "enable_hosted_draft_capability" {
  description = "Register the worker's dormant typed draft-and-approve capability gateway (ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY). No R0 policy grants R2 authority and no OAuth flow requests gmail.compose, so this stays inert even when on (docs/capability-gateway.md)."
  type        = bool
  default     = false
}

variable "enable_hosted_brief" {
  description = "Register the worker's proactive-brief executor and route (ATTUNE_ENABLE_HOSTED_BRIEF). Must be flipped together with the control-plane copy of this same-named variable in the edge root (docs/hosted-channels.md 'Proactive brief delivery')."
  type        = bool
  default     = false
}

variable "enable_tenant_model_profiles" {
  description = "Register per-tenant model profile support on the worker and model gateway together (ATTUNE_ENABLE_TENANT_MODEL_PROFILES). Must be flipped together with the control-plane copy of this same-named variable in the edge root (docs/hosted-model-profiles.md)."
  type        = bool
  default     = false
}

variable "enable_model_usage_metering" {
  description = "Register the worker's per-tenant model usage metering (ATTUNE_ENABLE_MODEL_USAGE_METERING). Independently activatable from the control-plane read route of the same name (docs/hosted-model-profiles.md)."
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

variable "slo_5xx_error_threshold" {
  description = "Runtime services page after more than this many 5xx responses within slo_alert_window_seconds. Conservative default; tune down only with evidence of real traffic volume."
  type        = number
  default     = 5

  validation {
    condition     = var.slo_5xx_error_threshold >= 1
    error_message = "slo_5xx_error_threshold must be at least 1."
  }
}

variable "slo_alert_window_seconds" {
  description = "Alignment window, in seconds, for the runtime 5xx-rate and p95-latency alert policies."
  type        = number
  default     = 300

  validation {
    condition     = var.slo_alert_window_seconds >= 60
    error_message = "slo_alert_window_seconds must be at least 60."
  }
}

variable "slo_worker_conversation_p95_latency_ms" {
  description = "The worker's bounded conversation-execution task kinds page when p95 execution latency exceeds this many milliseconds over slo_alert_window_seconds. Conversation tasks call the model gateway, so this is set well above a typical single-service timeout."
  type        = number
  default     = 15000

  validation {
    condition     = var.slo_worker_conversation_p95_latency_ms >= 1000
    error_message = "slo_worker_conversation_p95_latency_ms must be at least 1000."
  }
}

variable "labels" {
  description = "Additional non-sensitive resource labels."
  type        = map(string)
  default     = {}
}
