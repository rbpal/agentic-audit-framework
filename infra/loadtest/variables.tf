variable "location" {
  description = "Azure region. Match the durable stack so cross-stack latency to Azure OpenAI / Databricks is realistic."
  type        = string
  default     = "eastus2"
}

variable "resource_group_name" {
  description = "RG for the load-test stack. Deleting this RG cascades the AKS-owned MC_ node RG. Keep it distinct from the durable rg-agentic-audit-framework-dev."
  type        = string
  default     = "rg-aaf-loadtest-dev"
}

variable "name_prefix" {
  description = "Short prefix for resource names within this stack."
  type        = string
  default     = "aafload"
}

variable "node_vm_size" {
  description = <<-EOT
    AKS node VM size. B2ms (8GB) is the safe default for system pods +
    KEDA + API + mock-worker pods. B2s (4GB) halves the forgotten-cost
    (mock workers are tiny) at some scheduling risk — set it if minimising
    the bleed matters more than headroom.
  EOT
  type        = string
  default     = "Standard_B2ms"
}

variable "node_min_count" {
  description = "Cluster-autoscaler min nodes."
  type        = number
  default     = 1
}

variable "node_max_count" {
  description = "Cluster-autoscaler max nodes. 3 is enough to show a node scale-up under load without runaway cost."
  type        = number
  default     = 3
}

variable "servicebus_queue_name" {
  description = "Queue the API publishes to and KEDA scales the workers on. Must match SERVICEBUS_QUEUE_NAME in the app env."
  type        = string
  default     = "investigations"
}

variable "enable_front_door" {
  description = "Provision Azure Front Door in front of the AKS load balancer. Kept for resume breadth; the LB public IP alone is sufficient for a single-region test, so this can be disabled to simplify/save."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags on every resource — makes the throwaway stack easy to spot and audit."
  type        = map(string)
  default = {
    project    = "agentic-audit-framework"
    component  = "loadtest"
    lifecycle  = "transient-destroy-asap"
    managed_by = "terraform"
  }
}
