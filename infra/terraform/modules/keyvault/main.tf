# Azure Key Vault module.
#
# RBAC authorization model (NOT the legacy access-policy model).
# Decision documented in step_02_terraform_iac.md §3.5: RBAC is the
# Microsoft-recommended path, integrates with AAD role definitions,
# and supports just-in-time + conditional access. Access policies
# are deprecated in spirit even though still supported.
#
# Soft delete is mandatory (Azure no longer allows disabling it on
# vault create). Purge protection is opt-in here — enable for prod;
# leave off for dev so accidentally deleted vaults can be fully
# wiped within the soft-delete window if reused for testing.

resource "azurerm_key_vault" "this" {
  name                = var.vault_name
  location            = var.location
  resource_group_name = var.resource_group_name

  tenant_id = var.tenant_id
  sku_name  = "standard"

  rbac_authorization_enabled = true

  soft_delete_retention_days = var.soft_delete_retention_days
  purge_protection_enabled   = var.purge_protection_enabled

  public_network_access_enabled = var.public_network_access_enabled

  tags = var.tags
}
