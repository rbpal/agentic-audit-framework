output "endpoint_host_name" {
  description = "Front Door public hostname (https://<this>) — the Locust --host. Empty when enabled=false (use the LB IP directly)."
  value       = var.enabled ? azurerm_cdn_frontdoor_endpoint.this[0].host_name : ""
}
