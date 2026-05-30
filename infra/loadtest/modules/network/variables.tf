variable "name_prefix" {
  description = "Short prefix for resource names within this stack (e.g. vnet-<prefix>)."
  type        = string
}

variable "location" {
  description = "Azure region for the VNet."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group that holds the VNet and subnet."
  type        = string
}

variable "vnet_address_space" {
  description = "VNet address space. /16 leaves room for the Azure CNI pod subnet plus future subnets."
  type        = list(string)
  default     = ["10.224.0.0/16"]
}

variable "aks_subnet_prefixes" {
  description = "Address prefix(es) for the AKS node/pod subnet. /22 = 1024 IPs for a small autoscaling cluster under Azure CNI."
  type        = list(string)
  default     = ["10.224.0.0/22"]
}

variable "tags" {
  description = "Tags applied to taggable resources in this module."
  type        = map(string)
  default     = {}
}
