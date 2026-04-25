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
