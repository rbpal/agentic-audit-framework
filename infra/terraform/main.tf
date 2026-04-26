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

module "databricks" {
  source = "./modules/databricks"

  resource_group_name = azurerm_resource_group.app.name
  location            = azurerm_resource_group.app.location
  workspace_name      = "dbw-aaf-${var.environment}"
  tags                = local.common_tags
}

module "monitor" {
  source = "./modules/monitor"

  resource_group_name = azurerm_resource_group.app.name
  location            = azurerm_resource_group.app.location
  log_analytics_name  = "log-aaf-${var.environment}"
  app_insights_name   = "appi-aaf-${var.environment}"
  tags                = local.common_tags
}

module "keyvault" {
  source = "./modules/keyvault"

  resource_group_name = azurerm_resource_group.app.name
  location            = azurerm_resource_group.app.location
  # Key Vault names are globally unique — include name_suffix.
  vault_name = "kv-aaf-${var.name_suffix}-${var.environment}"
  tenant_id  = data.azurerm_client_config.current.tenant_id
  tags       = local.common_tags
}

# ── Inter-module wiring ──────────────────────────────────────────────

# Grant the Databricks Access Connector's system-assigned managed
# identity Storage Blob Data Contributor on the ADLS account.
#
# This is what wires Unity Catalog through the Access Connector to our
# ADLS storage. With this role:
#   * Unity Catalog can create / read / write managed tables and volumes
#     under any external location we register inside the ADLS account
#   * Storage credential federation works without storage account keys
#     (matches the project's "no API keys at runtime" stance from
#     step_02_terraform_iac.md §3.5)
#
# Scoped to the ADLS account (not subscription, not RG) — narrowest
# practical scope that still allows UC to manage every filesystem
# (bronze / silver / gold) it'll need. Could scope further to specific
# filesystems if we wanted per-tier credential separation, but that's
# overkill for this project's blast radius.
#
# This role assignment is the operational kickoff for step_03_task_03 —
# the metastore (Databricks-auto-created in step_03_task_02) gains the
# ability to actually reach our ADLS via the Access Connector after this
# applies.
resource "azurerm_role_assignment" "uc_access_connector_to_adls" {
  scope                = module.adls.account_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = module.databricks.access_connector_principal_id
}

# Unity Catalog storage credential + external locations for the
# project's ADLS Gen2 account. Created in step_03_task_04.
#
# This module produces:
#   * one databricks_storage_credential (federates UC → Access
#     Connector MSI → ADLS, no storage account keys involved)
#   * three databricks_external_location resources, one per medallion
#     filesystem, named bronze_<env>, silver_<env>, gold_<env>
#
# depends_on the role assignment above is critical: external location
# creation triggers a write-validation against ADLS, so the role grant
# MUST be applied first or apply fails with AuthorizationPermissionMismatch.
module "databricks_uc" {
  source = "./modules/databricks_uc"

  credential_name     = "aaf-${var.environment}-adls-cred"
  access_connector_id = module.databricks.access_connector_id

  external_locations = {
    "bronze_${var.environment}" = "abfss://bronze@${module.adls.account_name}.dfs.core.windows.net/"
    "silver_${var.environment}" = "abfss://silver@${module.adls.account_name}.dfs.core.windows.net/"
    "gold_${var.environment}"   = "abfss://gold@${module.adls.account_name}.dfs.core.windows.net/"
  }

  # step_03_task_05: catalog + schemas
  catalog_name      = "audit_${var.environment}"
  environment_label = var.environment

  schemas = {
    "bronze" = {
      comment      = "Raw landing — append-only; ingest writes here. Loose schema enforcement; SOX-evidence retention applies."
      storage_root = "abfss://bronze@${module.adls.account_name}.dfs.core.windows.net/"
    }
    "silver" = {
      comment      = "Cleaned + conformed entities — controls, attributes, evidence, validations. Strict schema; PII column masks attached."
      storage_root = "abfss://silver@${module.adls.account_name}.dfs.core.windows.net/"
    }
    "gold" = {
      comment      = "Curated for the agent + eval — claims, findings, telemetry. Heavy reads; partitioned for query performance."
      storage_root = "abfss://gold@${module.adls.account_name}.dfs.core.windows.net/"
    }
  }

  depends_on = [
    azurerm_role_assignment.uc_access_connector_to_adls,
  ]
}
