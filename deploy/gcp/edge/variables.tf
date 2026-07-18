variable "state_bucket" {
  description = "Private GCS bucket containing the foundation remote state."
  type        = string
}

variable "foundation_state_prefix" {
  description = "GCS prefix of the foundation Terraform state."
  type        = string
  default     = "foundation"
}

variable "runtime_state_prefix" {
  description = "GCS prefix of the runtime remote state containing the private OAuth exchange endpoint."
  type        = string
  default     = "runtime"
}

variable "control_plane_image" {
  description = "Artifact Registry control-plane image pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.control_plane_image))
    error_message = "control_plane_image must be an immutable @sha256 Artifact Registry reference."
  }
}

variable "oauth_callback_image" {
  description = "Artifact Registry dormant OAuth callback image pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.oauth_callback_image))
    error_message = "oauth_callback_image must be an immutable @sha256 Artifact Registry reference."
  }
}

variable "google_chat_ingress_image" {
  description = "Artifact Registry Google Chat ingress image pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.google_chat_ingress_image))
    error_message = "google_chat_ingress_image must be an immutable @sha256 Artifact Registry reference."
  }
}

variable "slack_ingress_image" {
  description = "Artifact Registry Slack ingress image pinned by sha256 digest; required only when the Slack ingress is deployed."
  type        = string
  default     = ""

  validation {
    condition     = var.slack_ingress_image == "" || can(regex("@sha256:[0-9a-f]{64}$", var.slack_ingress_image))
    error_message = "slack_ingress_image must be empty or an immutable @sha256 Artifact Registry reference."
  }
}

variable "export_download_image" {
  description = "Artifact Registry export download image pinned by sha256 digest."
  type        = string
  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.export_download_image))
    error_message = "export_download_image must be an immutable @sha256 Artifact Registry reference."
  }
}

variable "hostname" {
  description = "Exact lower-case public DNS hostname for the development edge."
  type        = string

  validation {
    condition = (
      length(var.hostname) >= 4 &&
      length(var.hostname) <= 253 &&
      can(regex(
        "^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$",
        var.hostname,
      ))
    )
    error_message = "hostname must be an exact lower-case DNS hostname."
  }
}

variable "labels" {
  description = "Additional non-sensitive resource labels."
  type        = map(string)
  default     = {}
}

variable "alert_notification_channels" {
  description = "Monitoring notification-channel resource names for edge security alerts."
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

variable "enable_identity_sign_in" {
  description = "Expose the staged Identity Platform session routes after provider and security evidence."
  type        = bool
  default     = false
}

variable "identity_provider_ready" {
  description = "Explicit operator attestation that the separate sign-in client and Identity Platform provider are configured and tested."
  type        = bool
  default     = false
}

variable "identity_api_key" {
  description = "Public Identity Platform browser API key; required only when staged sign-in is enabled."
  type        = string
  default     = ""

  validation {
    condition     = var.identity_api_key == "" || can(regex("^AIza[0-9A-Za-z_-]{35}$", var.identity_api_key))
    error_message = "identity_api_key must be empty or a syntactically valid public browser API key."
  }
}

variable "enable_google_workspace_oauth" {
  description = "Activate the separate Google Workspace connector-consent journey."
  type        = bool
  default     = false
}

variable "enable_hosted_onboarding" {
  description = "Expose the tenant-bound versioned hosted onboarding state API."
  type        = bool
  default     = false
}

variable "enable_hosted_policy" {
  description = "Expose the recent-authenticated fixed read-only policy ceremony."
  type        = bool
  default     = false
}

variable "enable_hosted_channels" {
  description = "Expose the recent-authenticated effect-free hosted channel preference ceremony."
  type        = bool
  default     = false
}

variable "enable_hosted_channel_setup" {
  description = "Expose the effect-free hosted channel installation setup boundary."
  type        = bool
  default     = false
}

variable "enable_hosted_channel_lifecycle" {
  description = "Expose the recent-authenticated hosted channel disconnect and replacement ceremony."
  type        = bool
  default     = false
}

variable "enable_customer_exports" {
  description = "Expose recent-authenticated account export requests and owner-bound status."
  type        = bool
  default     = false
}

variable "deploy_customer_export_download" {
  description = "Deploy the customer-export download service behind an unrouted, default-deny backend."
  type        = bool
  default     = false
}

variable "deploy_google_chat_ingress" {
  description = "Deploy the verified Google Chat ingress behind an unrouted load-balancer backend."
  type        = bool
  default     = false
}

variable "enable_google_chat_ingress" {
  description = "Route the exact Google Chat event endpoint after provider and adversarial evidence."
  type        = bool
  default     = false
}

variable "enable_google_chat_conversation" {
  description = "Route ordinary verified owner-DM messages into the bounded hosted conversation pipeline."
  type        = bool
  default     = false
}

variable "google_chat_provider_ready" {
  description = "Operator attestation that the platform Chat app uses the exact endpoint audience and passed negative tests."
  type        = bool
  default     = false
}

variable "google_chat_project_number" {
  description = "Public numeric project identity of the platform-owned Google Chat app."
  type        = string
  default     = ""

  validation {
    condition     = var.google_chat_project_number == "" || can(regex("^[1-9][0-9]{5,20}$", var.google_chat_project_number))
    error_message = "google_chat_project_number must be empty or a 6-21 digit nonzero project number."
  }
}

variable "deploy_slack_ingress" {
  description = "Deploy the signature-verified Slack ingress behind an unrouted load-balancer backend."
  type        = bool
  default     = false
}

variable "enable_slack_ingress" {
  description = "Route the exact Slack event endpoint after provider and adversarial evidence."
  type        = bool
  default     = false
}

variable "enable_slack_conversation" {
  description = "Route ordinary verified owner-DM Slack messages into the bounded hosted conversation pipeline."
  type        = bool
  default     = false
}

variable "slack_provider_ready" {
  description = "Operator attestation that the platform Slack app uses the exact event endpoint and passed negative tests."
  type        = bool
  default     = false
}

variable "google_oauth_provider_ready" {
  description = "Explicit operator attestation that the separate Workspace web client, exact redirect, consent screen, secret version, and negative tests are ready."
  type        = bool
  default     = false
}

variable "google_oauth_client_id" {
  description = "Public client ID of the separate Google Workspace OAuth web client."
  type        = string
  default     = ""

  validation {
    condition     = var.google_oauth_client_id == "" || can(regex("^[0-9]{6,32}-[0-9A-Za-z_-]{16,96}\\.apps\\.googleusercontent\\.com$", var.google_oauth_client_id))
    error_message = "google_oauth_client_id must be empty or a syntactically valid Google web client ID."
  }
}

variable "enable_hosted_slack_install" {
  description = "Expose hosted Slack app installation from the control plane; requires the runtime Slack channel and the deployed Slack ingress."
  type        = bool
  default     = false
}

variable "slack_client_id" {
  description = "Public client ID of the platform-owned Slack app; required only when hosted Slack installation is enabled."
  type        = string
  default     = ""

  validation {
    condition     = var.slack_client_id == "" || can(regex("^[0-9]{6,20}\\.[0-9]{6,20}$", var.slack_client_id))
    error_message = "slack_client_id must be empty or a syntactically valid Slack app client ID."
  }
}

variable "enable_hosted_web_conversation" {
  description = "Expose the identity-authenticated hosted web conversation message and turn-poll routes."
  type        = bool
  default     = false
}
