# Provider configurations.
#
# Subscription targeting: providers read ARM_SUBSCRIPTION_ID from the
# environment (set in operator shell config, never committed). Auth uses
# the active Azure CLI token by default — operators must `az login`
# before running terraform commands.

provider "azurerm" {
  features {}
}

provider "azapi" {
  # azapi inherits ARM_SUBSCRIPTION_ID + az-CLI auth automatically.
  # No explicit fields needed for local dev.
}

# Databricks workspace-level operations (Unity Catalog storage
# credentials, external locations, catalogs, schemas, tables, grants).
# Configured against the workspace from step_02_task_05.
#
# Auth: passes the workspace's Azure resource ID; the provider then
# uses Azure-AD auth via the operator's active `az login` session.
# No PATs, no service-principal secrets in source.
#
# The operator running terraform must be logged in as a principal
# with sufficient privilege on the metastore to create storage
# credentials and external locations. For step_03_task_04+ that
# means `palrb@rajendrabpalmsn.onmicrosoft.com` (Account Admin) —
# `az login` as that user before running `terraform apply` against
# this composition.
#
# This provider configuration depends on module.databricks already
# being in state (which it is, from step_02_task_05). On a fresh
# bootstrap with no state, this would fail; the workspace must
# exist first.
provider "databricks" {
  host                        = "https://${module.databricks.workspace_url}"
  azure_workspace_resource_id = module.databricks.workspace_id
}
