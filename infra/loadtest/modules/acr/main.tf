# Container registry + the pull grant for the cluster.
#
# Basic SKU is plenty for a single throwaway image. admin_enabled = false:
# the cluster pulls via its kubelet managed identity (AcrPull below), not a
# registry username/password.

resource "azurerm_container_registry" "this" {
  name                = "acr${var.name_prefix}"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = var.sku
  admin_enabled       = false
  tags                = var.tags
}

# Let the cluster pull from the registry (kubelet identity → AcrPull).
resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrPull"
  principal_id         = var.kubelet_principal_id
}
