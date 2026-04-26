# Unity Catalog storage credential + external locations.
#
# storage credential = the AAD identity Unity Catalog uses to reach
#   ADLS — in our case, the Databricks Access Connector's MSI
#   provisioned in step_02_task_05.
# external location = a UC name pinned to an ADLS prefix using
#   that credential. We declare three: one per medallion filesystem
#   (bronze, silver, gold).
#
# step_03_task_03 already granted the Access Connector MSI
# Storage Blob Data Contributor on the ADLS account, so this
# storage credential can actually read/write at apply time. The
# `depends_on` at the module call site ensures Terraform applies
# the role assignment before this module's resources.
#
# Once external locations exist, step_03_task_05 will create the
# audit_dev catalog with MANAGED LOCATION pointing at one of them
# (typically the bronze external location's prefix); each schema
# inside the catalog can also have its own MANAGED LOCATION.

resource "databricks_storage_credential" "adls" {
  name = var.credential_name

  azure_managed_identity {
    access_connector_id = var.access_connector_id
  }

  comment = "Unity Catalog storage credential — federates to ADLS via the Databricks Access Connector MSI. Created in step_03_task_04."
}

resource "databricks_external_location" "this" {
  for_each = var.external_locations

  name            = each.key
  url             = each.value
  credential_name = databricks_storage_credential.adls.name

  comment = "External location for the ${each.key} medallion tier. Created in step_03_task_04."
}
