variable "resource_group_name" {
  description = "Resource group to deploy the Key Vault into."
  type        = string
}

variable "location" {
  description = "Azure region."
  type        = string
}

variable "vault_name" {
  description = "Key Vault name. Globally unique across Azure; 3-24 chars, alphanumeric + hyphens; must start with a letter."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9-]{1,22}[a-zA-Z0-9]$", var.vault_name))
    error_message = "vault_name must start with a letter, end with letter/digit, 3-24 chars total, alphanumeric + hyphens only."
  }
}

variable "tenant_id" {
  description = "Tenant ID the vault is bound to. Pass `data.azurerm_client_config.current.tenant_id` from the root."
  type        = string
}

variable "soft_delete_retention_days" {
  description = "Days a soft-deleted vault is recoverable before permanent purge. Azure floor is 7, ceiling is 90."
  type        = number
  default     = 14

  validation {
    condition     = var.soft_delete_retention_days >= 7 && var.soft_delete_retention_days <= 90
    error_message = "soft_delete_retention_days must be between 7 and 90 (Azure rules)."
  }
}

variable "purge_protection_enabled" {
  description = "Whether purge protection is enabled. true = soft-deleted vaults CANNOT be force-purged; must wait for retention to expire. Recommended for prod; default off for dev so misconfigured vaults can be fully removed and recreated."
  type        = bool
  default     = false
}

variable "public_network_access_enabled" {
  description = "Whether the vault accepts public-internet traffic. true for portfolio dev; tighten via private endpoints + firewall for prod."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to every resource in the module."
  type        = map(string)
  default     = {}
}
