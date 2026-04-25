output "account_id" {
  description = "Resource ID of the Cognitive Services account. Use for role assignments in Task 06."
  value       = azurerm_cognitive_account.this.id
}

output "account_name" {
  description = "Name of the Cognitive Services account."
  value       = azurerm_cognitive_account.this.name
}

output "endpoint" {
  description = "Public HTTPS endpoint of the AOAI account. Base URL for Azure OpenAI client SDKs."
  value       = azurerm_cognitive_account.this.endpoint
}

output "principal_id" {
  description = "Object ID of the system-assigned managed identity on the AOAI account. Use for outbound role assignments (e.g. AOAI reading from Key Vault for CMK)."
  value       = azurerm_cognitive_account.this.identity[0].principal_id
}

output "deployment_name" {
  description = "Name of the GPT-4o deployment within the account."
  value       = azurerm_cognitive_deployment.gpt4o.name
}

output "deployment_id" {
  description = "Resource ID of the GPT-4o deployment."
  value       = azurerm_cognitive_deployment.gpt4o.id
}
