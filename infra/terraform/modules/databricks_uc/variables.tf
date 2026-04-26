variable "credential_name" {
  description = "Name of the Unity Catalog storage credential. Metastore-scoped; must be unique within the metastore. Convention for this project: aaf-<env>-adls-cred."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_-]{1,127}$", var.credential_name))
    error_message = "credential_name must start with a letter, be 2-128 chars, alphanumeric + hyphens + underscores."
  }
}

variable "access_connector_id" {
  description = "Azure resource ID of the Databricks Access Connector to use as the storage credential's managed identity. Pass module.databricks.access_connector_id from the root."
  type        = string
}

variable "external_locations" {
  description = "Map of external location name to the abfss:// URL it represents. Typically one entry per medallion tier: bronze_<env>, silver_<env>, gold_<env>."
  type        = map(string)

  validation {
    condition     = alltrue([for url in values(var.external_locations) : can(regex("^abfss://[a-z0-9]+@[a-z0-9]+\\.dfs\\.core\\.windows\\.net/", url))])
    error_message = "Every external_locations value must be an abfss:// URL of the form abfss://<filesystem>@<account>.dfs.core.windows.net/<path>/"
  }
}
