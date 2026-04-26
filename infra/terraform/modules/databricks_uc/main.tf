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

# Unity Catalog catalog + schemas (step_03_task_05).
#
# Catalog: one per env (audit_dev, audit_prod, …) — environment is the
# permission boundary. We do NOT specify a catalog-level managed
# location; each schema specifies its own to align storage with the
# medallion tier. The metastore has no default storage root either
# (see step_03_task_02 findings), so managed tables MUST go through
# schema-level managed locations.
#
# Schemas (bronze / silver / gold): one per medallion tier, each with
# its own managed_location pointing at the matching external location.
# Managed tables created in audit_dev.bronze land under bronze;
# audit_dev.silver under silver; audit_dev.gold under gold. Storage
# layout matches schema layout one-to-one — exactly the design
# committed to in step_03_task_01.
#
# force_destroy = false on both catalog and schemas: terraform destroy
# fails if there are objects inside. Safety against accidental wipes.
# To intentionally tear down dev, evacuate contents first (drop tables
# explicitly), then `terraform destroy`.

resource "databricks_catalog" "this" {
  name    = var.catalog_name
  comment = "Audit catalog for the ${var.environment_label} environment. Created in step_03_task_05; lifecycle managed by Terraform."

  force_destroy = false

  depends_on = [
    databricks_external_location.this,
  ]
}

resource "databricks_schema" "this" {
  for_each = var.schemas

  catalog_name = databricks_catalog.this.name
  name         = each.key
  comment      = each.value.comment
  storage_root = each.value.storage_root

  force_destroy = false
}
