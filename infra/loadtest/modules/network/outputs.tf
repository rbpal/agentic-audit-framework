output "aks_subnet_id" {
  description = "Subnet ID for the AKS default node pool (vnet_subnet_id)."
  value       = azurerm_subnet.aks.id
}

output "vnet_id" {
  description = "VNet resource ID."
  value       = azurerm_virtual_network.this.id
}

output "vnet_name" {
  description = "VNet name."
  value       = azurerm_virtual_network.this.name
}
