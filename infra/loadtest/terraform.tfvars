# Values for the transient AKS load-test stack. Auto-loaded by Terraform
# (file name `terraform.tfvars`), so the README's `terraform -chdir=infra/loadtest
# apply -auto-approve` picks these up with no -var-file flag.
#
# Safe to commit — NO secrets. Subscription / tenant come from the operator's
# ARM_SUBSCRIPTION_ID env var + `az login` (msn tenant), never from this file.
# The Service Bus connection string is a generated output, not an input.
#
# Every value here equals the variable default — listed explicitly so the whole
# stack's shape is visible in one place and tunable without touching .tf files.

# ── Stack identity ───────────────────────────────────────────────────
location            = "eastus2"
resource_group_name = "rg-aaf-loadtest-dev"
name_prefix         = "aafload"

# ── Network (Azure CNI) ──────────────────────────────────────────────
vnet_address_space  = ["10.224.0.0/16"]
aks_subnet_prefixes = ["10.224.0.0/22"]

# ── AKS ──────────────────────────────────────────────────────────────
aks_sku_tier   = "Free"          # $0 control plane, no SLA — fine for a ≤2-hr test
node_vm_size   = "Standard_B2ms" # B2s (4GB) trims cost further at scheduling risk
node_min_count = 1
# Cap at 3 to demo cluster-autoscaler node scale-up under load. Only a ceiling
# — you pay for nodes 2/3 only while they're actually running (pennies for a
# ≤2-hr test); idle stays at 1 node. Set to 1 for a hard single-node ceiling.
node_max_count = 3
keda_enabled   = true

# ── Container registry ───────────────────────────────────────────────
acr_sku = "Basic"

# ── Service Bus ──────────────────────────────────────────────────────
servicebus_sku                = "Basic"
servicebus_queue_name         = "investigations" # must match SERVICEBUS_QUEUE_NAME in the app
servicebus_max_delivery_count = 5

# ── Workload Identity (worker pods → Azure OpenAI, keyless) ──────────
workload_identity_namespace       = "default"
workload_identity_service_account = "audit-worker-sa"
azure_openai_role_name            = "Cognitive Services OpenAI User"
# azure_openai_account_id is NOT set here on purpose — a resource ID embeds the
# subscription GUID, which this committed file must not carry. Supply it via an
# env var ONLY for a REAL burst (mock-only leaves it empty → no durable-stack
# reference, role assignment = 0 resources):
#   export TF_VAR_azure_openai_account_id=$(az cognitiveservices account show \
#     -n aoai-aaf-rbpal-dev -g rg-agentic-audit-framework-dev --query id -o tsv)

# ── Front Door ───────────────────────────────────────────────────────
# Kept on for resume breadth. Set false to skip Front Door and drive the
# LB public IP directly (simpler, marginally cheaper).
enable_front_door = true

# ── Tags (every resource) ────────────────────────────────────────────
tags = {
  project    = "agentic-audit-framework"
  component  = "loadtest"
  lifecycle  = "transient-destroy-asap"
  managed_by = "terraform"
}
