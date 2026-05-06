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

variable "data_plane_user_principal_ids" {
  description = <<-EOT
    AAD principal object-IDs that should receive the
    `Cognitive Services OpenAI User` role on this Cognitive Services
    account. The role grants the
    `Microsoft.CognitiveServices/accounts/OpenAI/deployments/chat/completions/action`
    data action — i.e. the ability to call the chat completions API
    via Azure AD bearer token (NOT API keys, since `local_auth_enabled
    = false` is hardcoded above).

    Use this for **local-dev developers**: every user who runs
    `pytest -m slow tests/integration/test_layer2_generator_e2e.py`
    locally needs this role on the dev account, otherwise their
    `DefaultAzureCredential`-minted token gets a 401 PermissionDenied
    on the chat completions endpoint.

    Workload identities (Databricks job MSI for production runtime)
    should be granted separately — they need additional scopes that
    aren't appropriate to bundle here.

    Default empty so applying the module without specifying any user
    principals does not create role assignments. Discovered the need
    during step_05_task_03 cloud step when a manual `az role assignment
    create` had to be run to unblock the integration smoke test.

    Values are AAD object-IDs (GUIDs). Look up via:
      `az ad signed-in-user show --query id -o tsv`
  EOT
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for id in var.data_plane_user_principal_ids :
      can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", id))
    ])
    error_message = "Each principal_id must be a GUID (8-4-4-4-12 hex)."
  }
}
