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

variable "tags" {
  description = "Tags applied to every resource in the module."
  type        = map(string)
  default     = {}
}
