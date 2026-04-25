variable "resource_group_name" {
  description = "Resource group to deploy the ADLS storage account into."
  type        = string
}

variable "location" {
  description = "Azure region. GRS replication targets the paired region automatically."
  type        = string
}

variable "account_name" {
  description = "Storage account name. Globally unique across Azure; 3-24 chars, lowercase alphanumeric ONLY (no hyphens, no underscores)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.account_name))
    error_message = "account_name must be 3-24 chars, lowercase alphanumeric only — no hyphens or underscores (Azure storage account naming rules)."
  }
}

variable "replication_type" {
  description = "Storage account replication. GRS = geo-redundant (paired-region async); LRS = locally redundant. Project default GRS for application data — losing it means re-ingesting everything."
  type        = string
  default     = "GRS"

  validation {
    condition     = contains(["LRS", "ZRS", "GRS", "RAGRS", "GZRS", "RAGZRS"], var.replication_type)
    error_message = "replication_type must be one of: LRS, ZRS, GRS, RAGRS, GZRS, RAGZRS."
  }
}

variable "filesystems" {
  description = "Filesystems (Gen2 'containers') to create. Default = medallion architecture: bronze (raw), silver (cleaned), gold (curated)."
  type        = list(string)
  default     = ["bronze", "silver", "gold"]
}

variable "soft_delete_days" {
  description = "Soft-delete retention for blobs and containers (days). Recovers from accidental delete; modest blob storage cost overhead."
  type        = number
  default     = 14
}

variable "public_network_access_enabled" {
  description = "Whether the ADLS endpoint accepts public-internet traffic. true for portfolio dev (operator + Databricks default config); tighten via private endpoints + NSG for prod."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to every resource created by this module."
  type        = map(string)
  default     = {}
}
