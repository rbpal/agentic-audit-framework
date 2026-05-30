output "client_id" {
  description = "UAMI client ID — annotate the Kubernetes ServiceAccount with azure.workload.identity/client-id = this."
  value       = azurerm_user_assigned_identity.workload.client_id
}

output "principal_id" {
  description = "UAMI principal (object) ID — grant it the Azure OpenAI data-plane role (done at the root, count-gated)."
  value       = azurerm_user_assigned_identity.workload.principal_id
}

output "identity_name" {
  description = "User-assigned identity name."
  value       = azurerm_user_assigned_identity.workload.name
}
