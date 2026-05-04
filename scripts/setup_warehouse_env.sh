#!/usr/bin/env bash
# Interactive helper to set the three env vars our @pytest.mark.slow
# integration tests + scripts/run_layer1.py + scripts/benchmark_layer1.py
# need:
#
#   DATABRICKS_HOST              (workspace URL — non-secret)
#   DATABRICKS_SQL_WAREHOUSE_ID  (warehouse ID — non-secret)
#   DATABRICKS_TOKEN             (PAT — secret; read via `read -s`)
#
# Usage (MUST source, not execute):
#
#     source scripts/setup_warehouse_env.sh
#
# After sourcing, the env vars persist in your current shell so any
# subsequent `make integration-test-warehouse` / `poetry run python
# scripts/run_layer1.py` invocation picks them up.
#
# Why source-only: env-var assignments inside an executed subshell die
# when the subshell exits. `source` runs the script in the current
# shell so exports stick.
#
# This script is the canonical entry point for the workflow documented
# in CONTRIBUTING.md > "Running integration tests against a live
# Databricks warehouse".

# Refuse to run if executed instead of sourced. When sourced,
# BASH_SOURCE[0] differs from $0; when executed directly they match.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "ERROR: this script must be sourced, not executed."
    echo "Run it with:  source scripts/setup_warehouse_env.sh"
    exit 1
fi

# Defaults are dev-environment values. Override at the prompt or
# pre-set the env var before sourcing if you target a different
# workspace.
DEFAULT_HOST="https://adb-7405608712588657.17.azuredatabricks.net"
DEFAULT_WAREHOUSE_ID="dac9d7873e752cf0"

echo
echo "Databricks warehouse env-var setup"
echo "==================================="
echo

# DATABRICKS_HOST — non-secret, prompt with default
read -r -p "DATABRICKS_HOST [${DATABRICKS_HOST:-$DEFAULT_HOST}]: " input_host
export DATABRICKS_HOST="${input_host:-${DATABRICKS_HOST:-$DEFAULT_HOST}}"

# DATABRICKS_SQL_WAREHOUSE_ID — non-secret, prompt with default
read -r -p "DATABRICKS_SQL_WAREHOUSE_ID [${DATABRICKS_SQL_WAREHOUSE_ID:-$DEFAULT_WAREHOUSE_ID}]: " input_warehouse
export DATABRICKS_SQL_WAREHOUSE_ID="${input_warehouse:-${DATABRICKS_SQL_WAREHOUSE_ID:-$DEFAULT_WAREHOUSE_ID}}"

# DATABRICKS_TOKEN — SECRET, silent input via -s. Token never echoes
# to terminal; never lands in shell history (because it's read input,
# not a typed command).
echo
echo "DATABRICKS_TOKEN — generate via Databricks UI:"
echo "  Workspace → Settings → Developer → Access tokens → Generate"
echo "  Scope: BI Tools (NOT 'Other APIs')"
echo "  Lifetime: 1 day for one-off runs; longer for ongoing dev"
echo
read -r -s -p "DATABRICKS_TOKEN: " input_token
export DATABRICKS_TOKEN="$input_token"
unset input_token  # don't leave the cleartext in a shell-local var
echo

# Verification — print HOST + WAREHOUSE_ID values (non-secret) and
# token length only (NEVER the token value).
echo
echo "Verification"
echo "------------"
[[ -n "$DATABRICKS_HOST" ]] && echo "HOST:      $DATABRICKS_HOST" || echo "HOST:      MISSING"
[[ -n "$DATABRICKS_SQL_WAREHOUSE_ID" ]] && echo "WAREHOUSE: $DATABRICKS_SQL_WAREHOUSE_ID" || echo "WAREHOUSE: MISSING"
if [[ -n "$DATABRICKS_TOKEN" ]]; then
    echo "TOKEN:     set (${#DATABRICKS_TOKEN} chars)"
else
    echo "TOKEN:     MISSING"
fi
echo
echo "Ready to run:"
echo "  make integration-test-warehouse"
echo "  poetry run python scripts/run_layer1.py"
echo "  poetry run pytest -m slow tests/integration/ -v"
echo
