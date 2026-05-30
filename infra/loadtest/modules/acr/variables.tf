variable "name_prefix" {
  description = "Short prefix for the registry name (acr<prefix> — no hyphens; ACR names are alphanumeric only)."
  type        = string
}

variable "location" {
  description = "Azure region for the registry."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group that holds the container registry."
  type        = string
}

variable "sku" {
  description = "Container registry SKU. Basic suffices for one transient image."
  type        = string
  default     = "Basic"
}

variable "kubelet_principal_id" {
  description = "Object ID of the AKS kubelet identity, granted AcrPull on this registry. Comes from the aks module."
  type        = string
}

variable "tags" {
  description = "Tags applied to the registry."
  type        = map(string)
  default     = {}
}
