#!/usr/bin/env bash
#
# remote_state_bootstrap.sh
# -------------------------
# Step 2 / Task 01 — bootstraps the Azure resources Terraform's azurerm
# backend writes its state into.
#
# Creates (idempotently):
#   * Resource group:    rg-terraform-state
#   * Storage account:   stagenticauditstate<suffix>  (Standard_GRS, StorageV2)
#   * Container:         tfstate                       (private)
# With:
#   * Blob versioning enabled
#   * Blob soft delete enabled (14-day retention)
#   * Container soft delete enabled (14-day retention)
#   * Public blob access disabled
#   * Minimum TLS 1.2
#
# Usage:
#   az login                                    # one-time
#   az account set --subscription <id>          # if you have multiple
#   bash infra/bootstrap/remote_state_bootstrap.sh
#
# Optional environment overrides:
#   LOCATION             (default: eastus2)
#   SUFFIX               (default: derived from `whoami`)
#   SOFT_DELETE_DAYS     (default: 14)
#   WRITE_BACKEND_CONF   (default: 1; set to 0 to skip writing backend.conf)
#
# Idempotent: re-running prints "...already exists, skipping create" for
# each existing resource and exits 0 without touching them.

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────

RG_NAME="rg-terraform-state"
LOCATION="${LOCATION:-eastus2}"
CONTAINER_NAME="tfstate"
SOFT_DELETE_DAYS="${SOFT_DELETE_DAYS:-14}"

# Default suffix: 1-6 lowercase alphanumeric chars derived from whoami.
# Override via SUFFIX env var if it collides with another Azure account
# or if you want a stable shared suffix across machines.
DEFAULT_SUFFIX="$(whoami | tr -dc 'a-z0-9' | head -c 6)"
SUFFIX="${SUFFIX:-$DEFAULT_SUFFIX}"
STORAGE_NAME="stagenticauditstate${SUFFIX}"

# Where to write the backend config the next Terraform task will consume
BACKEND_CONF_PATH="infra/terraform/envs/dev/backend.conf"

# ── Logging ──────────────────────────────────────────────────────────

if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    NC=''
fi

log_info() { printf "%b[INFO]%b %s\n" "$GREEN" "$NC" "$1"; }
log_warn() { printf "%b[WARN]%b %s\n" "$YELLOW" "$NC" "$1"; }
log_err()  { printf "%b[ERR ]%b %s\n" "$RED" "$NC" "$1" >&2; }

# ── Pre-flight checks ────────────────────────────────────────────────

if ! command -v az >/dev/null 2>&1; then
    log_err "Azure CLI ('az') not found. Install: https://aka.ms/azcli"
    exit 1
fi

if ! az account show >/dev/null 2>&1; then
    log_err "Not logged in to Azure CLI. Run: az login"
    exit 1
fi

# Storage account name validation: 3-24 lowercase alphanumeric (Azure rule)
if ! [[ "$STORAGE_NAME" =~ ^[a-z0-9]{3,24}$ ]]; then
    log_err "Storage account name '$STORAGE_NAME' is invalid"
    log_err "Must be 3-24 chars, lowercase alphanumeric only"
    log_err "Adjust via SUFFIX env var (current suffix: '$SUFFIX')"
    exit 1
fi

SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
SUBSCRIPTION_NAME="$(az account show --query name -o tsv)"

log_info "Subscription:    $SUBSCRIPTION_NAME ($SUBSCRIPTION_ID)"
log_info "Region:          $LOCATION"
log_info "Resource group:  $RG_NAME"
log_info "Storage account: $STORAGE_NAME"
log_info "Container:       $CONTAINER_NAME"
log_info "Soft delete:     ${SOFT_DELETE_DAYS} days"
echo

# ── 1. Resource group ────────────────────────────────────────────────

if az group show --name "$RG_NAME" >/dev/null 2>&1; then
    log_info "Resource group '$RG_NAME' already exists, skipping create"
else
    log_info "Creating resource group '$RG_NAME' in $LOCATION..."
    az group create \
        --name "$RG_NAME" \
        --location "$LOCATION" \
        --tags purpose=terraform-state \
               owner="$(whoami)" \
               project=agentic-audit-framework \
               managed_by=remote_state_bootstrap.sh \
        --output none
    log_info "  ✓ Resource group created"
fi

# ── 2. Storage account ───────────────────────────────────────────────

if az storage account show \
        --name "$STORAGE_NAME" \
        --resource-group "$RG_NAME" >/dev/null 2>&1; then
    log_info "Storage account '$STORAGE_NAME' already exists, skipping create"
else
    log_info "Creating storage account '$STORAGE_NAME'..."
    az storage account create \
        --name "$STORAGE_NAME" \
        --resource-group "$RG_NAME" \
        --location "$LOCATION" \
        --sku Standard_GRS \
        --kind StorageV2 \
        --min-tls-version TLS1_2 \
        --allow-blob-public-access false \
        --output none
    log_info "  ✓ Storage account created"
fi

# ── 3. Blob versioning + soft delete (idempotent — PATCH semantics) ──

log_info "Configuring blob versioning + soft delete..."
az storage account blob-service-properties update \
    --account-name "$STORAGE_NAME" \
    --resource-group "$RG_NAME" \
    --enable-versioning true \
    --enable-delete-retention true \
    --delete-retention-days "$SOFT_DELETE_DAYS" \
    --enable-container-delete-retention true \
    --container-delete-retention-days "$SOFT_DELETE_DAYS" \
    --output none
log_info "  ✓ Versioning ON; blob + container soft delete ON (${SOFT_DELETE_DAYS}d)"

# ── 4. Container ─────────────────────────────────────────────────────

# Use --auth-mode login so we don't need to fetch storage keys.
# Requires the running user to have "Storage Blob Data Contributor"
# on the storage account, which is auto-granted to subscription
# owners + contributors.
if az storage container show \
        --name "$CONTAINER_NAME" \
        --account-name "$STORAGE_NAME" \
        --auth-mode login >/dev/null 2>&1; then
    log_info "Container '$CONTAINER_NAME' already exists, skipping create"
else
    log_info "Creating container '$CONTAINER_NAME'..."
    az storage container create \
        --name "$CONTAINER_NAME" \
        --account-name "$STORAGE_NAME" \
        --auth-mode login \
        --public-access off \
        --output none
    log_info "  ✓ Container created"
fi

# ── 5. Output backend config ─────────────────────────────────────────

echo
log_info "Bootstrap complete. Terraform backend config:"
echo
cat <<EOF
# infra/terraform/envs/dev/backend.conf
# Generated by remote_state_bootstrap.sh — re-run that script to refresh.
resource_group_name  = "$RG_NAME"
storage_account_name = "$STORAGE_NAME"
container_name       = "$CONTAINER_NAME"
key                  = "agentic-audit-framework/dev.tfstate"
EOF
echo

# Optionally write the backend.conf to disk for the next Terraform task
if [[ "${WRITE_BACKEND_CONF:-1}" == "1" ]]; then
    mkdir -p "$(dirname "$BACKEND_CONF_PATH")"
    cat > "$BACKEND_CONF_PATH" <<EOF
# Generated by infra/bootstrap/remote_state_bootstrap.sh.
# Safe to commit — contains no secrets.
resource_group_name  = "$RG_NAME"
storage_account_name = "$STORAGE_NAME"
container_name       = "$CONTAINER_NAME"
key                  = "agentic-audit-framework/dev.tfstate"
EOF
    log_info "Wrote $BACKEND_CONF_PATH"
fi

log_info "Done."
