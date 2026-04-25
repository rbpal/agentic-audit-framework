#!/usr/bin/env bash
#
# verify_dev.sh — end-to-end verification of the Step 2 Terraform
# composition against the live dev environment.
#
# Runs in two phases:
#   1. terraform plan  → expect "No changes" (proves source matches state)
#   2. az queries      → confirm each Azure resource exists with the
#                        attributes documented in
#                        step_02_terraform_iac.md
#
# Idempotent. Read-only. Safe to run anytime.
#
# Usage:
#   az login                            # one-time
#   bash infra/verify_dev.sh
#
# Exit code 0 = all checks pass. Non-zero = at least one drift / missing
# resource / unexpected attribute. Per-check failures are printed inline
# for triage.

set -uo pipefail

# ── Configuration ───────────────────────────────────────────────────

STATE_RG="rg-terraform-state"
STATE_SA="stagenticauditstaterbpal"
APP_RG="rg-agentic-audit-framework-dev"
ADLS_SA="dlsaafrbpaldev"
AOAI_ACCOUNT="aoai-aaf-dev"
DBW="dbw-aaf-dev"
LA_WS="log-aaf-dev"
APPI="appi-aaf-dev"
KV="kv-aaf-rbpal-dev"

# ── Logging ─────────────────────────────────────────────────────────

if [[ -t 1 ]]; then
    GREEN='\033[0;32m'
    RED='\033[0;31m'
    NC='\033[0m'
else
    GREEN=''; RED=''; NC=''
fi

PASS=0
FAIL=0

ok() { printf "%b✓%b %s\n" "$GREEN" "$NC" "$1"; PASS=$((PASS + 1)); }
err() { printf "%b✗%b %s\n" "$RED" "$NC" "$1"; FAIL=$((FAIL + 1)); }

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        ok "$label = $actual"
    else
        err "$label: expected '$expected', got '$actual'"
    fi
}

# ── Phase 1: terraform plan must say "No changes" ───────────────────

echo
echo "── Phase 1: terraform plan against live state ──"
PLAN_OUT="$(terraform -chdir=infra/terraform plan \
    -var-file=envs/dev/terraform.tfvars -no-color 2>&1)"
if grep -q "No changes" <<< "$PLAN_OUT"; then
    ok "terraform plan: No changes (source matches state)"
else
    err "terraform plan reports drift — run a real plan and investigate"
    echo "$PLAN_OUT" | tail -20
fi

# ── Phase 2: az queries per resource ────────────────────────────────

echo
echo "── Phase 2: per-resource verification via az ──"

# State backend (Task 01)
assert_eq "rg-terraform-state.location" "eastus2" \
    "$(az group show --name "$STATE_RG" --query location -o tsv 2>/dev/null)"
assert_eq "stagenticauditstaterbpal.kind" "StorageV2" \
    "$(az storage account show --name "$STATE_SA" -g "$STATE_RG" \
        --query kind -o tsv 2>/dev/null)"
assert_eq "stagenticauditstaterbpal.replication" "Standard_GRS" \
    "$(az storage account show --name "$STATE_SA" -g "$STATE_RG" \
        --query sku.name -o tsv 2>/dev/null)"
assert_eq "stagenticauditstaterbpal.versioning" "true" \
    "$(az storage account blob-service-properties show \
        --account-name "$STATE_SA" -g "$STATE_RG" \
        --query isVersioningEnabled -o tsv 2>/dev/null)"

# App RG (Task 03)
assert_eq "$APP_RG.location" "eastus2" \
    "$(az group show --name "$APP_RG" --query location -o tsv 2>/dev/null)"

# Azure OpenAI (Task 03)
assert_eq "$AOAI_ACCOUNT.kind" "OpenAI" \
    "$(az cognitiveservices account show --name "$AOAI_ACCOUNT" -g "$APP_RG" \
        --query kind -o tsv 2>/dev/null)"
assert_eq "$AOAI_ACCOUNT.disable_local_auth" "true" \
    "$(az cognitiveservices account show --name "$AOAI_ACCOUNT" -g "$APP_RG" \
        --query properties.disableLocalAuth -o tsv 2>/dev/null)"
assert_eq "$AOAI_ACCOUNT.identity" "SystemAssigned" \
    "$(az cognitiveservices account show --name "$AOAI_ACCOUNT" -g "$APP_RG" \
        --query identity.type -o tsv 2>/dev/null)"
assert_eq "gpt-4o.model_version" "2024-11-20" \
    "$(az cognitiveservices account deployment show \
        --name "$AOAI_ACCOUNT" -g "$APP_RG" \
        --deployment-name gpt-4o \
        --query properties.model.version -o tsv 2>/dev/null)"
assert_eq "gpt-4o.upgrade_option" "NoAutoUpgrade" \
    "$(az cognitiveservices account deployment show \
        --name "$AOAI_ACCOUNT" -g "$APP_RG" \
        --deployment-name gpt-4o \
        --query properties.versionUpgradeOption -o tsv 2>/dev/null)"

# ADLS Gen2 (Task 04)
assert_eq "$ADLS_SA.kind" "StorageV2" \
    "$(az storage account show --name "$ADLS_SA" -g "$APP_RG" \
        --query kind -o tsv 2>/dev/null)"
assert_eq "$ADLS_SA.hns" "true" \
    "$(az storage account show --name "$ADLS_SA" -g "$APP_RG" \
        --query isHnsEnabled -o tsv 2>/dev/null)"
assert_eq "$ADLS_SA.replication" "Standard_GRS" \
    "$(az storage account show --name "$ADLS_SA" -g "$APP_RG" \
        --query sku.name -o tsv 2>/dev/null)"
for fs in bronze silver gold; do
    assert_eq "$ADLS_SA.filesystem.$fs" "$fs" \
        "$(az storage fs show --name "$fs" \
            --account-name "$ADLS_SA" --auth-mode login \
            --query name -o tsv 2>/dev/null)"
done

# Databricks (Task 05)
assert_eq "$DBW.sku" "premium" \
    "$(az databricks workspace show --name "$DBW" -g "$APP_RG" \
        --query sku.name -o tsv 2>/dev/null)"
assert_eq "$DBW-connector.identity" "SystemAssigned" \
    "$(az databricks access-connector show --name "${DBW}-connector" -g "$APP_RG" \
        --query identity.type -o tsv 2>/dev/null)"

# Monitor (Task 06)
assert_eq "$LA_WS.sku" "PerGB2018" \
    "$(az monitor log-analytics workspace show --workspace-name "$LA_WS" -g "$APP_RG" \
        --query sku.name -o tsv 2>/dev/null)"
assert_eq "$APPI.kind" "web" \
    "$(az monitor app-insights component show --app "$APPI" -g "$APP_RG" \
        --query applicationType -o tsv 2>/dev/null)"

# Key Vault (Task 06)
assert_eq "$KV.rbac" "true" \
    "$(az keyvault show --name "$KV" -g "$APP_RG" \
        --query properties.enableRbacAuthorization -o tsv 2>/dev/null)"
assert_eq "$KV.soft_delete_days" "14" \
    "$(az keyvault show --name "$KV" -g "$APP_RG" \
        --query properties.softDeleteRetentionInDays -o tsv 2>/dev/null)"

# ── Summary ─────────────────────────────────────────────────────────

echo
echo "── Summary ──"
printf "Passed: %d\nFailed: %d\n" "$PASS" "$FAIL"
if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
echo "Step 2 dev environment is consistent end-to-end."
