variable "environment" {
  description = "Deployment environment name. Used in resource naming and tagging."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be one of: dev, prod"
  }
}

variable "location" {
  description = "Azure region for primary resources. Paired-region replication targets are derived per resource."
  type        = string
  default     = "eastus2"
}

variable "owner" {
  description = "Owner identifier (email or username) for resource tagging."
  type        = string
  default     = "rbpal"
}

variable "extra_tags" {
  description = "Additional tags merged onto every resource via local.common_tags."
  type        = map(string)
  default     = {}
}

variable "name_suffix" {
  description = "Stable identifier appended to globally-unique resource names (Azure DNS-scoped: AOAI custom subdomain, ADLS account, etc.). Mirrors the bootstrap script's SUFFIX."
  type        = string
  default     = "rbpal"

  validation {
    condition     = can(regex("^[a-z0-9]{2,12}$", var.name_suffix))
    error_message = "name_suffix must be 2-12 chars, lowercase alphanumeric (Azure DNS rules)."
  }
}

# ── Task 03: Azure OpenAI ────────────────────────────────────────────

variable "openai_account_name" {
  description = "Cognitive Services (Azure OpenAI) account name. RG-scoped uniqueness — does not need name_suffix appended."
  type        = string
  default     = "aoai-aaf-dev"
}

variable "openai_model_version" {
  description = "GPT-4o version pin. Pinning deliberately prevents silent model drift across plan/apply cycles; bump in a separate PR to validate behaviour."
  type        = string

  # Bumped 2026-04-25: 2024-08-06 hit Azure's CREATION-deprecation
  # threshold on 2026-03-31 (new deployments rejected with HTTP 400
  # ServiceModelDeprecated) even though it still appeared in
  # `az cognitiveservices model list` with INFERENCE deprecation
  # 2026-10-01. Lesson: the model list reports inference (calls
  # against existing deployments) availability, not creation (new
  # deployments) availability — the two windows can diverge by months.
  default = "2024-11-20"
}

variable "openai_model_capacity_tpm" {
  description = "GPT-4o capacity in thousands of tokens-per-minute. 10 = 10K TPM. Subject to subscription quota; raise via portal request before bumping here."
  type        = number
  default     = 10
}
