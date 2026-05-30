# Root composition for the transient AKS load-test stack.
#
# Everything lives under a single RG created here (not inside a module) so one
# `terraform -chdir=infra/loadtest destroy` — or `az group delete -n <rg>` —
# cleans up the whole stack in one shot and cascades the AKS-owned MC_ node RG.
# State is LOCAL and there are ZERO references to the durable infra/terraform/
# stack: separate state + separate RG = two independent teardown safety nets.
#
# Per-stack values come from terraform.tfvars (auto-loaded). Module defaults
# make every value optional, so the stack still applies with an empty tfvars.

resource "azurerm_resource_group" "loadtest" {
  name     = var.resource_group_name
  location = var.location
  tags     = var.tags
}

# ── Network (Azure CNI) ──────────────────────────────────────────────
module "network" {
  source = "./modules/network"

  name_prefix         = var.name_prefix
  location            = azurerm_resource_group.loadtest.location
  resource_group_name = azurerm_resource_group.loadtest.name
  vnet_address_space  = var.vnet_address_space
  aks_subnet_prefixes = var.aks_subnet_prefixes
  tags                = var.tags
}

# ── AKS (cluster + shared static LB public IP) ───────────────────────
module "aks" {
  source = "./modules/aks"

  name_prefix         = var.name_prefix
  location            = azurerm_resource_group.loadtest.location
  resource_group_name = azurerm_resource_group.loadtest.name
  subnet_id           = module.network.aks_subnet_id
  sku_tier            = var.aks_sku_tier
  node_vm_size        = var.node_vm_size
  node_min_count      = var.node_min_count
  node_max_count      = var.node_max_count
  keda_enabled        = var.keda_enabled
  tags                = var.tags
}

# ── Container registry (+ AcrPull for the cluster's kubelet identity) ─
module "acr" {
  source = "./modules/acr"

  name_prefix          = var.name_prefix
  location             = azurerm_resource_group.loadtest.location
  resource_group_name  = azurerm_resource_group.loadtest.name
  sku                  = var.acr_sku
  kubelet_principal_id = module.aks.kubelet_object_id
  tags                 = var.tags
}

# ── Service Bus (async job buffer + KEDA scale trigger) ──────────────
module "servicebus" {
  source = "./modules/servicebus"

  name_prefix         = var.name_prefix
  location            = azurerm_resource_group.loadtest.location
  resource_group_name = azurerm_resource_group.loadtest.name
  sku                 = var.servicebus_sku
  queue_name          = var.servicebus_queue_name
  max_delivery_count  = var.servicebus_max_delivery_count
  tags                = var.tags
}

# ── Workload Identity (worker pods → Azure OpenAI, keyless) ──────────
# UAMI + federated credential trusting the cluster's OIDC issuer for the
# worker ServiceAccount. The identity is created unconditionally (free, and
# the SA annotation output is always available); only the role grant below
# touches the durable stack.
module "workload_identity" {
  source = "./modules/workload_identity"

  name_prefix               = var.name_prefix
  resource_group_name       = azurerm_resource_group.loadtest.name
  location                  = azurerm_resource_group.loadtest.location
  oidc_issuer_url           = module.aks.oidc_issuer_url
  service_account_namespace = var.workload_identity_namespace
  service_account_name      = var.workload_identity_service_account
  tags                      = var.tags
}

# ── Cross-stack grant: workload identity → durable Azure OpenAI ──────
# THE ONLY reference to the durable stack, and it's count-gated: with
# azure_openai_account_id = "" (the default, mock-only) this is zero
# resources and the loadtest stack stays fully self-contained. Supply the
# durable OpenAI account's resource ID (via TF_VAR_azure_openai_account_id)
# to enable REAL-mode keyless auth. `destroy` only ever removes this role
# assignment — it never touches the OpenAI account itself.
resource "azurerm_role_assignment" "workload_to_openai" {
  count                = var.azure_openai_account_id == "" ? 0 : 1
  scope                = var.azure_openai_account_id
  role_definition_name = var.azure_openai_role_name
  principal_id         = module.workload_identity.principal_id
}

# ── Front Door (optional global edge over the LB public IP) ──────────
module "frontdoor" {
  source = "./modules/frontdoor"

  name_prefix         = var.name_prefix
  resource_group_name = azurerm_resource_group.loadtest.name
  enabled             = var.enable_front_door
  origin_ip           = module.aks.lb_public_ip
  tags                = var.tags
}
