output "log_analytics_workspace_id" {
  description = "Resource ID of the Log Analytics workspace. Use as `workspace_id` on every diagnostic_setting in downstream modules."
  value       = azurerm_log_analytics_workspace.this.id
}

output "log_analytics_workspace_name" {
  description = "Name of the Log Analytics workspace."
  value       = azurerm_log_analytics_workspace.this.name
}

output "app_insights_id" {
  description = "Resource ID of the Application Insights resource."
  value       = azurerm_application_insights.this.id
}

output "app_insights_connection_string" {
  description = "App Insights connection string. Set as APPLICATIONINSIGHTS_CONNECTION_STRING env var on the agent / pipeline runtime to ship telemetry."
  value       = azurerm_application_insights.this.connection_string
  sensitive   = true
}

output "app_insights_instrumentation_key" {
  description = "App Insights instrumentation key. Legacy; prefer connection_string. Exposed only because some SDKs still default to it."
  value       = azurerm_application_insights.this.instrumentation_key
  sensitive   = true
}
