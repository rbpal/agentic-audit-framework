variable "resource_group_name" {
  description = "Resource group for both the Log Analytics workspace and Application Insights resource."
  type        = string
}

variable "location" {
  description = "Azure region."
  type        = string
}

variable "log_analytics_name" {
  description = "Log Analytics workspace name. 4-63 chars, alphanumeric + hyphens; must start and end with alphanumeric."
  type        = string
}

variable "app_insights_name" {
  description = "Application Insights resource name."
  type        = string
}

variable "retention_in_days" {
  description = "Telemetry retention. 30 = the floor on PerGB2018 SKU; raise as data-retention requirements grow."
  type        = number
  default     = 30
}

variable "daily_quota_gb" {
  description = <<-EOT
    Daily ingestion cap (GB) for the Log Analytics workspace — a cost
    tripwire. Once the cap is hit, non-essential ingestion pauses until
    the next UTC day (telemetry can be dropped), so it bounds worst-case
    spend on a credit-capped subscription. -1 = uncapped (Azure default).

    Default 1 GB/day (~30 GB/mo) sits far above this project's normal
    Layer-3 span volume (well under the 5 GB/mo free grant) but stops a
    runaway loop from quietly draining the balance. Raise or set -1 if a
    legitimate high-volume run needs it.
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.daily_quota_gb == -1 || var.daily_quota_gb > 0
    error_message = "daily_quota_gb must be -1 (uncapped) or a positive number of GB."
  }
}

variable "tags" {
  description = "Tags applied to every resource in the module."
  type        = map(string)
  default     = {}
}

# ── Alerting (action group + scheduled-query rules) ──────────────────

variable "action_group_name" {
  description = "Name of the Action Group that fans alert notifications out to receivers. RG-scoped uniqueness. Convention: ag-aaf-<env>."
  type        = string
}

variable "action_group_short_name" {
  description = "Action Group short name — used as the SMS/email sender prefix. Azure caps this at 12 characters. Convention: aaf-<env>."
  type        = string

  validation {
    condition     = length(var.action_group_short_name) <= 12
    error_message = "action_group_short_name must be 12 characters or fewer (Azure limit)."
  }
}

variable "operator_email" {
  description = <<-EOT
    Destination address for the Action Group's single email receiver —
    the human who gets paged when a scheduled-query alert fires. No
    default: each environment must inject its own operator in tfvars so
    a stray apply never silently drops alerts on the floor. Production
    would swap this email receiver for a webhook → PagerDuty/OpsGenie.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.operator_email))
    error_message = "operator_email must be a valid email address."
  }
}
