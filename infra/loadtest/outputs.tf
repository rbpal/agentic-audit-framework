# Output names are unchanged from the pre-module layout — the README's
# `terraform output -raw <name>` calls keep working as-is. Values now read
# through the modules instead of root-level resources.

output "resource_group_name" {
  description = "The load-test RG. `az group delete -n <this>` cascades the MC_ node RG — the teardown panic button."
  value       = azurerm_resource_group.loadtest.name
}

output "aks_node_resource_group" {
  description = "AKS-owned node RG (holds the node VMs, LB, the static IP). Deleted automatically when the RG above is deleted."
  value       = module.aks.node_resource_group
}

output "aks_name" {
  description = "AKS cluster name — for `az aks get-credentials -g <rg> -n <this>`."
  value       = module.aks.cluster_name
}

output "acr_login_server" {
  description = "ACR login server — `az acr build -r <this> -t audit-api:load .` then reference <this>/audit-api:load in the manifests."
  value       = module.acr.login_server
}

output "acr_name" {
  description = "ACR name for `az acr build -r <this>`."
  value       = module.acr.name
}

output "lb_public_ip" {
  description = "Static public IP shared by the AKS LoadBalancer Service (loadBalancerIP) and the Front Door origin."
  value       = module.aks.lb_public_ip
}

output "servicebus_queue_name" {
  description = "Queue name — set SERVICEBUS_QUEUE_NAME to this in the app/worker."
  value       = module.servicebus.queue_name
}

output "servicebus_connection_string" {
  description = "Namespace connection string for the app (SERVICEBUS_CONNECTION_STRING) and the KEDA TriggerAuthentication. Sensitive."
  value       = module.servicebus.primary_connection_string
  sensitive   = true
}

output "workload_identity_client_id" {
  description = "UAMI client ID — fill the ServiceAccount annotation: `export WORKLOAD_IDENTITY_CLIENT_ID=$(terraform … output -raw workload_identity_client_id)` then envsubst k8s/serviceaccount.yaml."
  value       = module.workload_identity.client_id
}

output "workload_identity_service_account" {
  description = "Kubernetes ServiceAccount name the worker pods run under (annotated with the UAMI client ID)."
  value       = var.workload_identity_service_account
}

output "openai_role_assigned" {
  description = "True when the workload identity has been granted the Azure OpenAI data-plane role (i.e. azure_openai_account_id was supplied). False = mock-only / no durable-stack reference."
  value       = var.azure_openai_account_id != ""
}

output "front_door_endpoint" {
  description = "Front Door public hostname (https://<this>) — the Locust --host. Empty when enable_front_door=false (use lb_public_ip directly)."
  value       = module.frontdoor.endpoint_host_name
}
