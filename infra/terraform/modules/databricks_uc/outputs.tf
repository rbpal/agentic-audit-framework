output "storage_credential_name" {
  description = "Name of the Unity Catalog storage credential. Reference this from databricks_external_location resources and from CREATE CATALOG ... USING CREDENTIAL clauses."
  value       = databricks_storage_credential.adls.name
}

output "storage_credential_id" {
  description = "ID of the storage credential. Useful for databricks_grants resources that scope grants to the credential itself."
  value       = databricks_storage_credential.adls.id
}

output "external_location_names" {
  description = "Map of medallion tier (bronze / silver / gold) to external location name. step_03_task_05 catalog/schema definitions will reference these by name when declaring MANAGED LOCATIONs."
  value       = { for k, v in databricks_external_location.this : k => v.name }
}

output "external_location_urls" {
  description = "Map of medallion tier to ADLS URL. Useful for cross-checking against the ADLS module's filesystems."
  value       = { for k, v in databricks_external_location.this : k => v.url }
}
