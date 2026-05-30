variable "name_prefix" {
  description = "Short prefix for resource names within this stack (aks-<prefix>, pip-<prefix>-lb)."
  type        = string
}

variable "location" {
  description = "Azure region for the cluster and public IP."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group that holds the AKS cluster. (The node VMs / LB / public IP live in the AKS-owned MC_ node RG.)"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID for the default node pool (Azure CNI). Comes from the network module."
  type        = string
}

variable "sku_tier" {
  description = "AKS control-plane SKU tier. Free = $0, no uptime SLA — fine for a transient load test. Standard adds the SLA."
  type        = string
  default     = "Free"
}

variable "node_vm_size" {
  description = "AKS node VM size for the default node pool."
  type        = string
  default     = "Standard_B2ms"
}

variable "node_min_count" {
  description = "Cluster-autoscaler min nodes."
  type        = number
  default     = 1
}

variable "node_max_count" {
  description = "Cluster-autoscaler max nodes."
  type        = number
  default     = 3
}

variable "keda_enabled" {
  description = "Enable the managed KEDA add-on (event-driven autoscaling of the worker Deployment on Service Bus queue depth)."
  type        = bool
  default     = true
}

variable "workload_identity_enabled" {
  description = "Enable OIDC issuer + Workload Identity on the cluster, so worker pods can call Azure OpenAI via a federated UAMI (DefaultAzureCredential, no key). Harmless for mock-only runs."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to the cluster and public IP."
  type        = map(string)
  default     = {}
}
