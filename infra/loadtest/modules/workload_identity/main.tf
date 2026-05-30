# AKS Workload Identity — the AAD identity the worker pods assume to call
# Azure OpenAI with DefaultAzureCredential (no API key, no secret).
#
# Two pieces:
#   * a user-assigned managed identity (UAMI) — the Azure-side identity, and
#   * a federated credential that trusts tokens minted by the cluster's OIDC
#     issuer for one specific Kubernetes ServiceAccount.
#
# The trust is scoped by `subject` to exactly system:serviceaccount:<ns>:<sa>,
# so only pods running under that ServiceAccount (and labelled
# azure.workload.identity/use=true) can exchange a projected token for an AAD
# token as this UAMI. The role grant that makes the identity *useful* (OpenAI
# data-plane access) is a separate, count-gated role assignment at the root.

resource "azurerm_user_assigned_identity" "workload" {
  name                = "id-${var.name_prefix}-workload"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "workload" {
  name                = "fic-${var.name_prefix}-worker"
  resource_group_name = var.resource_group_name
  parent_id           = azurerm_user_assigned_identity.workload.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = var.oidc_issuer_url
  subject             = "system:serviceaccount:${var.service_account_namespace}:${var.service_account_name}"
}
