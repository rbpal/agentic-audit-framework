variable "name_prefix" {
  description = "Short prefix for identity resource names (id-<prefix>-workload, fic-<prefix>-worker)."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group that holds the user-assigned identity."
  type        = string
}

variable "location" {
  description = "Azure region for the user-assigned identity."
  type        = string
}

variable "oidc_issuer_url" {
  description = "AKS cluster OIDC issuer URL (from the aks module). The federated credential trusts tokens minted by this issuer."
  type        = string
}

variable "service_account_namespace" {
  description = "Kubernetes namespace of the worker ServiceAccount the credential is federated to."
  type        = string
  default     = "default"
}

variable "service_account_name" {
  description = "Kubernetes ServiceAccount name the credential is federated to. Must match the SA annotated with this UAMI's client_id and used by the worker pods."
  type        = string
  default     = "audit-worker-sa"
}

variable "tags" {
  description = "Tags applied to the user-assigned identity."
  type        = map(string)
  default     = {}
}
