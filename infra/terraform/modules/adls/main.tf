# Azure Data Lake Storage Gen2 module.
#
# Creates a StorageV2 account with hierarchical namespace enabled (the
# "Gen2" flag) plus the medallion-architecture filesystems consumed by
# the pipeline:
#   - bronze: raw landing for the ingest layer
#   - silver: cleaned / conformed (Delta tables in Task 05+)
#   - gold:   curated aggregates surfaced to the agent
#
# This is the project's primary durable application data store. State
# storage (Task 01) holds operational Terraform metadata; this account
# holds application data.

resource "azurerm_storage_account" "adls" {
  name                = var.account_name
  location            = var.location
  resource_group_name = var.resource_group_name

  account_kind             = "StorageV2"
  account_tier             = "Standard"
  account_replication_type = var.replication_type

  # Hierarchical namespace — the "Gen2" flag. Real directory operations
  # (rename / list / delete) become O(1) metadata ops instead of O(N)
  # blob enumerations. Required for any tool that expects a real
  # filesystem (Spark, Databricks Unity Catalog, ADF).
  is_hns_enabled = true

  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false

  public_network_access_enabled = var.public_network_access_enabled

  # NOTE: shared_access_key_enabled defaults to true. Disabling it is
  # the proper hardening but requires (a) provider-level
  # `storage_use_azuread = true` and (b) explicit Storage Blob Data
  # Contributor role assignment to the Terraform-running principal.
  # Both land in a Task 04 hardening follow-up PR — same pattern as
  # Task 03's hardening cycle.

  blob_properties {
    # NOTE: versioning_enabled is INTENTIONALLY omitted. Azure rejects
    # `versioning_enabled = true` when `is_hns_enabled = true` —
    # ADLS Gen2 and blob versioning are mutually exclusive features.
    # Discovered at apply time on 2026-04-25 (HTTP error from the
    # Azure storage API). For ADLS Gen2 protection, rely on:
    #   - container_delete_retention_policy (below) for accidental delete
    #   - delete_retention_policy (below) for accidental blob delete
    #   - HNS itself (atomic directory ops; lower risk of accidental
    #     bulk delete vs flat blob namespace)
    # If point-in-time restore is needed later, look at change_feed +
    # restore_policy (HNS-compatible).

    delete_retention_policy {
      days = var.soft_delete_days
    }

    container_delete_retention_policy {
      days = var.soft_delete_days
    }
  }

  tags = var.tags
}

resource "azurerm_storage_data_lake_gen2_filesystem" "this" {
  for_each = toset(var.filesystems)

  name               = each.key
  storage_account_id = azurerm_storage_account.adls.id
}
