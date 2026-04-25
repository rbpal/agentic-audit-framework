# Observability backbone — Log Analytics + workspace-based App Insights.
#
# All diagnostic settings (Cognitive Services, ADLS, Databricks, Key
# Vault) eventually point at this Log Analytics workspace; App
# Insights is the application-telemetry surface (custom events,
# requests, dependencies, exceptions).
#
# We use the *workspace-based* App Insights model — required since
# late 2020; the classic standalone model is being retired.

resource "azurerm_log_analytics_workspace" "this" {
  name                = var.log_analytics_name
  location            = var.location
  resource_group_name = var.resource_group_name

  sku               = "PerGB2018"
  retention_in_days = var.retention_in_days

  tags = var.tags
}

resource "azurerm_application_insights" "this" {
  name                = var.app_insights_name
  location            = var.location
  resource_group_name = var.resource_group_name

  application_type  = "web"
  workspace_id      = azurerm_log_analytics_workspace.this.id
  retention_in_days = var.retention_in_days

  tags = var.tags
}
