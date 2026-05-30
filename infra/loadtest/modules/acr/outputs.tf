output "login_server" {
  description = "ACR login server — `az acr build -r <name> -t audit-api:load .` then reference <this>/audit-api:load in the manifests."
  value       = azurerm_container_registry.this.login_server
}

output "name" {
  description = "ACR name for `az acr build -r <this>`."
  value       = azurerm_container_registry.this.name
}

output "id" {
  description = "ACR resource ID."
  value       = azurerm_container_registry.this.id
}
