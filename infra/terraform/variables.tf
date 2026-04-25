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
}

variable "extra_tags" {
  description = "Additional tags merged onto every resource via local.common_tags."
  type        = map(string)
  default     = {}
}
