output "vault_id" {
  description = "Resource ID of the Key Vault. Use for role assignments (e.g. Key Vault Secrets User on the agent's managed identity)."
  value       = azurerm_key_vault.this.id
}

output "vault_name" {
  description = "Name of the Key Vault."
  value       = azurerm_key_vault.this.name
}

output "vault_uri" {
  description = "Vault URI — https://<vault>.vault.azure.net/. Pass to the Azure SDK / Databricks secret scope as the resource URL."
  value       = azurerm_key_vault.this.vault_uri
}
