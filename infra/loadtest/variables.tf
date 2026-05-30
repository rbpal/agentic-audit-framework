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

# ── Network module ───────────────────────────────────────────────────
variable "vnet_address_space" {
  description = "VNet address space (Azure CNI). /16 leaves headroom for the pod subnet."
  type        = list(string)
  default     = ["10.224.0.0/16"]
}

variable "aks_subnet_prefixes" {
  description = "AKS node/pod subnet prefix(es). /22 = 1024 IPs for a small autoscaling cluster under Azure CNI."
  type        = list(string)
  default     = ["10.224.0.0/22"]
}

# ── AKS module ───────────────────────────────────────────────────────
variable "aks_sku_tier" {
  description = "AKS control-plane SKU tier. Free = $0, no uptime SLA (fine for a ≤2-hr test)."
  type        = string
  default     = "Free"
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

variable "keda_enabled" {
  description = "Enable the managed KEDA add-on (scales the worker Deployment on Service Bus queue depth)."
  type        = bool
  default     = true
}

# ── ACR module ───────────────────────────────────────────────────────
variable "acr_sku" {
  description = "Container registry SKU. Basic suffices for one transient image."
  type        = string
  default     = "Basic"
}

# ── Service Bus module ───────────────────────────────────────────────
variable "servicebus_sku" {
  description = "Service Bus namespace SKU. Basic supports queues with no monthly base charge."
  type        = string
  default     = "Basic"
}

variable "servicebus_queue_name" {
  description = "Queue the API publishes to and KEDA scales the workers on. Must match SERVICEBUS_QUEUE_NAME in the app env."
  type        = string
  default     = "investigations"
}

variable "servicebus_max_delivery_count" {
  description = "Max delivery attempts before a message dead-letters — stops a poison message looping forever during the test."
  type        = number
  default     = 5
}

# ── Workload Identity (worker pods → Azure OpenAI, keyless) ──────────
variable "workload_identity_namespace" {
  description = "Kubernetes namespace of the worker ServiceAccount federated to the UAMI. Manifests deploy to 'default'."
  type        = string
  default     = "default"
}

variable "workload_identity_service_account" {
  description = "Kubernetes ServiceAccount name the UAMI is federated to. Must match k8s/serviceaccount.yaml and the worker pod's serviceAccountName."
  type        = string
  default     = "audit-worker-sa"
}

variable "azure_openai_account_id" {
  description = <<-EOT
    Resource ID of the DURABLE-stack Azure OpenAI account to grant the
    workload identity data-plane access on. THE ONLY reference to the durable
    stack — left empty by default so a mock-only run stays fully isolated
    (the role assignment becomes zero resources). Supply it via an env var to
    avoid committing a subscription ID to tfvars:
      export TF_VAR_azure_openai_account_id=$(az cognitiveservices account show \
        -n aoai-aaf-rbpal-dev -g rg-agentic-audit-framework-dev --query id -o tsv)
  EOT
  type        = string
  default     = ""
}

variable "azure_openai_role_name" {
  description = "Built-in role granted to the workload identity on the Azure OpenAI account. Data-plane inference access."
  type        = string
  default     = "Cognitive Services OpenAI User"
}

# ── Front Door module ────────────────────────────────────────────────
variable "enable_front_door" {
  description = "Provision Azure Front Door in front of the AKS load balancer. Kept for resume breadth; the LB public IP alone is sufficient for a single-region test, so this can be disabled to simplify/save."
  type        = bool
  default     = true
}

# ── Tags (all resources) ─────────────────────────────────────────────
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
