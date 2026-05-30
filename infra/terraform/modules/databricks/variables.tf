variable "resource_group_name" {
  description = "Resource group to deploy the workspace + access connector into."
  type        = string
}

variable "location" {
  description = "Azure region. Premium SKU + Unity Catalog availability varies; eastus2 is broadly supported."
  type        = string
}

variable "workspace_name" {
  description = "Databricks workspace name. 3-30 chars, alphanumeric + hyphens; must start and end with alphanumeric."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9-]{1,28}[a-zA-Z0-9]$", var.workspace_name))
    error_message = "workspace_name must be 3-30 chars, alphanumeric + hyphens, starting and ending with alphanumeric."
  }
}

variable "tags" {
  description = "Tags applied to every resource in the module."
  type        = map(string)
  default     = {}
}

variable "log_analytics_workspace_id" {
  description = <<-EOT
    Resource ID of the Log Analytics workspace to forward the workspace's
    diagnostic logs to (typically the monitor module's
    `log_analytics_workspace_id` output → `log-aaf-<env>`). When set, a
    single diagnostic setting ships a focused category set
    (unityCatalog / clusters / jobs / notebook) to that workspace,
    enabling cross-stack KQL that correlates audit-framework spans with
    Databricks events. Defaults null → no diagnostic setting created, so
    the module stays usable standalone (forwarding is opt-in).
  EOT
  type        = string
  default     = null
}
