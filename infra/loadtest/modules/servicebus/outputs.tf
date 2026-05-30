output "queue_name" {
  description = "Queue name — set SERVICEBUS_QUEUE_NAME to this in the app/worker."
  value       = azurerm_servicebus_queue.this.name
}

output "namespace_id" {
  description = "Service Bus namespace resource ID."
  value       = azurerm_servicebus_namespace.this.id
}

output "primary_connection_string" {
  description = "Namespace connection string for the app (SERVICEBUS_CONNECTION_STRING) and the KEDA TriggerAuthentication. Sensitive."
  value       = azurerm_servicebus_namespace.this.default_primary_connection_string
  sensitive   = true
}
