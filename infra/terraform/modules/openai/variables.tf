variable "resource_group_name" {
  description = "Resource group to deploy the Cognitive Services account into."
  type        = string
}

variable "location" {
  description = "Azure region. GPT-4o is GA in eastus2, eastus, swedencentral, westus3 — confirm regional model availability before changing."
  type        = string
}

variable "account_name" {
  description = "Cognitive Services account name. RG-scoped; 2-64 chars, must start with a letter and end with letter/digit."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9-]{0,62}[a-zA-Z0-9]$", var.account_name))
    error_message = "account_name must start with a letter, end with letter/digit, 2-64 chars total, alphanumeric and hyphens only."
  }
}

variable "custom_subdomain_name" {
  description = "DNS subdomain for the AOAI endpoint — becomes https://<this>.openai.azure.com/. Globally unique across Azure. Required for Azure AD / managed-identity auth."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$", var.custom_subdomain_name))
    error_message = "custom_subdomain_name must be 2-64 chars, lowercase alphanumeric and hyphens, start/end with alphanumeric (Azure DNS rules)."
  }
}

variable "deployment_name" {
  description = "Name of the model deployment within the OpenAI account. Becomes the deployment ID clients pass to the SDK."
  type        = string
  default     = "gpt-4o"
}

variable "model_version" {
  description = "GPT-4o model version. Pinning prevents silent drift across plan/apply runs."
  type        = string
  default     = "2024-08-06"
}

variable "model_capacity_tpm" {
  description = "Tokens-per-minute capacity in thousands. 10 = 10K TPM. Subject to subscription quota."
  type        = number
  default     = 10
}

variable "public_network_access_enabled" {
  description = "Whether the AOAI endpoint accepts public-internet traffic. true for portfolio dev; tighten via private endpoints + NSG for prod."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to every resource created by this module."
  type        = map(string)
  default     = {}
}
