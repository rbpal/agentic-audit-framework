# Azure Databricks workspace module.
#
# Creates a Premium-SKU Databricks workspace (required for Unity
# Catalog, IP access lists, customer-managed keys, and credential
# passthrough) plus an Access Connector — the managed-identity
# resource Databricks uses to reach ADLS without storage account
# keys. The Access Connector is the cleanest path to wire the
# workspace into ADLS Gen2 (Task 04) for Unity Catalog storage
# credentials.
#
# Metastore wiring (account-level resource that Unity Catalog needs)
# is intentionally NOT in this module — it requires Databricks
# account-level auth, lives at the region level, and is typically
# set up once per region across all workspaces. Defer to a Step 3+
# task once the metastore identity / governance model is decided.

resource "azurerm_databricks_workspace" "this" {
  name                = var.workspace_name
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "premium"

  # Managed RG is created by Azure for Databricks-internal resources
  # (VNet, NSG, NICs for the cluster nodes). We name it explicitly
  # so it's discoverable in the portal and cleanly destroy-able.
  managed_resource_group_name = "${var.resource_group_name}-databricks-managed"

  tags = var.tags
}

resource "azurerm_databricks_access_connector" "this" {
  name                = "${var.workspace_name}-connector"
  resource_group_name = var.resource_group_name
  location            = var.location

  identity {
    type = "SystemAssigned"
  }

  tags = var.tags
}
