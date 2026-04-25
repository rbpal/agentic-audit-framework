output "workspace_id" {
  description = "Resource ID of the Databricks workspace."
  value       = azurerm_databricks_workspace.this.id
}

output "workspace_url" {
  description = "Workspace URL — https://<workspace-id>.azuredatabricks.net. Use to log into the Databricks UI and as the host for the databricks Terraform provider in later tasks."
  value       = azurerm_databricks_workspace.this.workspace_url
}

output "workspace_name" {
  description = "Name of the Databricks workspace."
  value       = azurerm_databricks_workspace.this.name
}

output "access_connector_id" {
  description = "Resource ID of the Access Connector. Use as `databricks_storage_credential.azure_managed_identity.access_connector_id` when wiring the workspace into ADLS via Unity Catalog."
  value       = azurerm_databricks_access_connector.this.id
}

output "access_connector_principal_id" {
  description = "Object ID of the Access Connector's system-assigned managed identity. Grant this principal Storage Blob Data Contributor on the ADLS account / containers it should reach."
  value       = azurerm_databricks_access_connector.this.identity[0].principal_id
}
