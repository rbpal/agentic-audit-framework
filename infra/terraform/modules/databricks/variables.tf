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
