output "cluster_name" {
  description = "AKS cluster name — for `az aks get-credentials -g <rg> -n <this>`."
  value       = azurerm_kubernetes_cluster.this.name
}

output "node_resource_group" {
  description = "AKS-owned node RG (holds the node VMs, LB, the static IP). Deleted automatically when the parent RG is deleted."
  value       = azurerm_kubernetes_cluster.this.node_resource_group
}

output "kubelet_object_id" {
  description = "Object ID of the cluster's kubelet identity — grant it AcrPull so nodes can pull images."
  value       = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
}

output "lb_public_ip" {
  description = "Static public IP shared by the AKS LoadBalancer Service (loadBalancerIP) and the Front Door origin."
  value       = azurerm_public_ip.lb.ip_address
}

output "lb_public_ip_name" {
  description = "Name of the static public IP (= pip-<name_prefix>-lb) — referenced by the K8s Service annotation."
  value       = azurerm_public_ip.lb.name
}

output "oidc_issuer_url" {
  description = "Cluster OIDC issuer URL — the federated-identity-credential issuer for Workload Identity. Empty when workload_identity_enabled = false."
  value       = var.workload_identity_enabled ? azurerm_kubernetes_cluster.this.oidc_issuer_url : ""
}
