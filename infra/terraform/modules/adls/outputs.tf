output "account_id" {
  description = "Resource ID of the ADLS storage account. Use for role assignments in Task 06."
  value       = azurerm_storage_account.adls.id
}

output "account_name" {
  description = "Name of the ADLS storage account."
  value       = azurerm_storage_account.adls.name
}

output "primary_dfs_endpoint" {
  description = "Primary DFS (Gen2) endpoint, https://<account>.dfs.core.windows.net/. Use as base URL for Databricks Spark connectors and ADLS SDKs that prefer the Gen2 namespace."
  value       = azurerm_storage_account.adls.primary_dfs_endpoint
}

output "primary_blob_endpoint" {
  description = "Primary blob endpoint, https://<account>.blob.core.windows.net/. ADLS Gen2 supports both DFS and blob endpoints; some legacy SDKs default to blob."
  value       = azurerm_storage_account.adls.primary_blob_endpoint
}

output "filesystem_ids" {
  description = "Map of filesystem name to resource ID. Use for downstream role assignments (e.g. Storage Blob Data Contributor on a specific filesystem)."
  value       = { for k, v in azurerm_storage_data_lake_gen2_filesystem.this : k => v.id }
}
