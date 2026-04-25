# Root composition.
#
# Resource modules will be wired in by Tasks 03–06:
#   * Task 03 — module "openai"
#   * Task 04 — module "adls"
#   * Task 05 — module "databricks"
#   * Task 06 — module "monitor" + module "keyvault"
#
# Per-environment values are supplied at apply time via
#   -var-file=envs/<env>/terraform.tfvars   (Task 07).

# Subscription / tenant context. Used by downstream modules for role
# assignments, Key Vault tenant binding, and diagnostic resource scoping.
data "azurerm_client_config" "current" {}

# Common tags applied to every resource via merge() at the module level.
# Centralising tag policy here keeps governance consistent across modules
# and lets us add an audit tag (cost-centre, compliance-tier, etc.)
# without editing every resource block.
locals {
  common_tags = merge(
    {
      project     = "agentic-audit-framework"
      environment = var.environment
      managed_by  = "terraform"
      owner       = var.owner
    },
    var.extra_tags,
  )
}

# App resource group. Holds every Terraform-managed resource for this
# environment. Created at the root (not inside any module) so a single
# `terraform destroy` cleans up everything in one shot. The state RG
# (rg-terraform-state) is bootstrap-managed and stays out of Terraform's
# purview by design — see step_02_terraform_iac.md §6 for the rationale.
resource "azurerm_resource_group" "app" {
  name     = "rg-agentic-audit-framework-${var.environment}"
  location = var.location
  tags     = local.common_tags
}

# ── Modules ──────────────────────────────────────────────────────────

module "openai" {
  source = "./modules/openai"

  resource_group_name   = azurerm_resource_group.app.name
  location              = azurerm_resource_group.app.location
  account_name          = var.openai_account_name
  custom_subdomain_name = "aoai-aaf-${var.name_suffix}-${var.environment}"
  model_version         = var.openai_model_version
  model_capacity_tpm    = var.openai_model_capacity_tpm
  tags                  = local.common_tags
}

module "adls" {
  source = "./modules/adls"

  resource_group_name = azurerm_resource_group.app.name
  location            = azurerm_resource_group.app.location
  # Storage account names disallow hyphens — concatenated lowercase only.
  # Pattern: dls (CAF abbreviation) + aaf (project) + suffix + env.
  account_name = "dlsaaf${var.name_suffix}${var.environment}"
  tags         = local.common_tags
}
