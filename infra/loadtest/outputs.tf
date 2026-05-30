output "resource_group_name" {
  description = "The load-test RG. `az group delete -n <this>` cascades the MC_ node RG — the teardown panic button."
  value       = azurerm_resource_group.loadtest.name
}

output "aks_node_resource_group" {
  description = "AKS-owned node RG (holds the node VMs, LB, the static IP). Deleted automatically when the RG above is deleted."
  value       = azurerm_kubernetes_cluster.this.node_resource_group
}

output "aks_name" {
  description = "AKS cluster name — for `az aks get-credentials -g <rg> -n <this>`."
  value       = azurerm_kubernetes_cluster.this.name
}

output "acr_login_server" {
  description = "ACR login server — `az acr build -r <this> -t audit-api:load .` then reference <this>/audit-api:load in the manifests."
  value       = azurerm_container_registry.this.login_server
}

output "acr_name" {
  description = "ACR name for `az acr build -r <this>`."
  value       = azurerm_container_registry.this.name
}

output "lb_public_ip" {
  description = "Static public IP shared by the AKS LoadBalancer Service (loadBalancerIP) and the Front Door origin."
  value       = azurerm_public_ip.lb.ip_address
}

output "servicebus_queue_name" {
  description = "Queue name — set SERVICEBUS_QUEUE_NAME to this in the app/worker."
  value       = azurerm_servicebus_queue.investigations.name
}

output "servicebus_connection_string" {
  description = "Namespace connection string for the app (SERVICEBUS_CONNECTION_STRING) and the KEDA TriggerAuthentication. Sensitive."
  value       = azurerm_servicebus_namespace.this.default_primary_connection_string
  sensitive   = true
}

output "front_door_endpoint" {
  description = "Front Door public hostname (https://<this>) — the Locust --host. Empty when enable_front_door=false (use lb_public_ip directly)."
  value       = var.enable_front_door ? azurerm_cdn_frontdoor_endpoint.this[0].host_name : ""
}
