variable "name_prefix" {
  description = "Short prefix for Front Door resource names (afd-<prefix>, fde-<prefix>)."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group that holds the Front Door profile. (Front Door is a global service; only the profile is RG-scoped.)"
  type        = string
}

variable "enabled" {
  description = "Provision Front Door. When false the whole module collapses to zero resources and endpoint_host_name is empty."
  type        = bool
  default     = true
}

variable "origin_ip" {
  description = "Origin host — the AKS LB static public IP. Raw IP, so the route forwards over HTTP and cert-name check is disabled."
  type        = string
}

variable "health_probe_path" {
  description = "Origin health-probe path."
  type        = string
  default     = "/healthz"
}

variable "tags" {
  description = "Tags applied to the Front Door profile/endpoint."
  type        = map(string)
  default     = {}
}
