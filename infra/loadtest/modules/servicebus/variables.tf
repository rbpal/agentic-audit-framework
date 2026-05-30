variable "name_prefix" {
  description = "Short prefix for the namespace name (sb-<prefix>)."
  type        = string
}

variable "location" {
  description = "Azure region for the Service Bus namespace."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group that holds the Service Bus namespace."
  type        = string
}

variable "sku" {
  description = "Service Bus namespace SKU. Basic supports queues (all this test needs) with no monthly base charge."
  type        = string
  default     = "Basic"
}

variable "queue_name" {
  description = "Queue the API publishes to and KEDA scales the workers on. Must match SERVICEBUS_QUEUE_NAME in the app env."
  type        = string
  default     = "investigations"
}

variable "max_delivery_count" {
  description = "Max delivery attempts before a message dead-letters — keeps a poison message from looping forever during the test."
  type        = number
  default     = 5
}

variable "tags" {
  description = "Tags applied to the namespace."
  type        = map(string)
  default     = {}
}
