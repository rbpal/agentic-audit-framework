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

# ── step_03_task_05 inputs ───────────────────────────────────────────

variable "catalog_name" {
  description = "Unity Catalog catalog name. Metastore-scoped; convention `audit_<env>` (audit_dev, audit_prod). The catalog is the permission boundary for the environment — one GRANT cascades to every schema and table inside."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{1,254}$", var.catalog_name))
    error_message = "catalog_name must start with a letter, be 2-255 chars, alphanumeric + underscores only (UC catalog naming rules)."
  }
}

variable "environment_label" {
  description = "Human-readable environment label used in resource comments (e.g., 'dev', 'prod'). Just for docstrings; doesn't influence resource naming."
  type        = string
}

variable "catalog_storage_root" {
  description = "Catalog-level managed_location. Required — the metastore has no default storage root, so Databricks rejects catalog creation without one. Convention: pass one of the external location URLs (typically bronze). Any schema whose managed_location is that same URL must omit storage_root and inherit; schemas with different URLs must NOT be sub-paths of this."
  type        = string

  validation {
    condition     = can(regex("^abfss://[a-z0-9]+@[a-z0-9]+\\.dfs\\.core\\.windows\\.net/", var.catalog_storage_root))
    error_message = "catalog_storage_root must be an abfss:// URL."
  }
}

variable "schemas" {
  description = "Map of schema name → {comment, storage_root}. storage_root is optional (use null for schemas that should inherit the catalog's managed_location). For schemas whose tier matches the catalog's managed_location URL, set storage_root = null and inherit; for schemas with their own tier path, set explicitly."
  type = map(object({
    comment      = string
    storage_root = optional(string)
  }))

  validation {
    condition     = alltrue([for s in values(var.schemas) : s.storage_root == null || can(regex("^abfss://[a-z0-9]+@[a-z0-9]+\\.dfs\\.core\\.windows\\.net/", s.storage_root))])
    error_message = "Each schema's storage_root must be either null (inherit catalog's managed_location) or an abfss:// URL."
  }
}
