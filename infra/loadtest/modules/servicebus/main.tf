# Service Bus — the async job buffer + backpressure. Basic tier: no monthly
# base, queues only (all we need), pennies per million ops.

resource "azurerm_servicebus_namespace" "this" {
  name                = "sb-${var.name_prefix}"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = var.sku
  tags                = var.tags
}

resource "azurerm_servicebus_queue" "this" {
  name         = var.queue_name
  namespace_id = azurerm_servicebus_namespace.this.id

  # Short-lived demo: let abandoned/failed messages dead-letter quickly so a
  # poison message can't loop forever during the test.
  max_delivery_count = var.max_delivery_count
}
