# Azure CNI network for the AKS cluster. With Azure CNI, pods get real VNet
# IPs from the subnet below, so the address space must be sized for pod count
# (not just node count). /22 = 1024 IPs — ample for a 1-3 node,
# max-pods-default cluster.

resource "azurerm_virtual_network" "this" {
  name                = "vnet-${var.name_prefix}"
  location            = var.location
  resource_group_name = var.resource_group_name
  address_space       = var.vnet_address_space
  tags                = var.tags
}

resource "azurerm_subnet" "aks" {
  name                 = "snet-aks"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = var.aks_subnet_prefixes
}
