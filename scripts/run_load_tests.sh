#!/usr/bin/env bash
# Step-11 load test — sequential mock-mode scale points with cooldown.
#
# Drives the deployed API (Front Door URL) at increasing concurrency so KEDA
# (worker pods on queue depth) and the HPA (API pods on CPU) visibly scale.
# Mock mode means $0 LLM spend, so we can push real concurrency. Exports
# per-scale CSV that scripts/analyze_load_test.py reads.
#
# Usage:
#   HOST=https://<name>.azurefd.net bash scripts/run_load_tests.sh
#   (or pass the LB public IP: HOST=http://20.62.x.x)
#
# Requires locust on PATH (pipx install locust). Watch scaling meanwhile:
#   kubectl get hpa,pods,nodes -w
set -euo pipefail

HOST="${HOST:?set HOST to the Front Door URL or LB public IP}"
OUTDIR="${OUTDIR:-privateDocs/load_test_results}"
mkdir -p "$OUTDIR"

# scale points: "<users> <spawn-rate> <duration>"
SCALES=(
  "50 5 2m"
  "200 20 5m"
  "500 50 5m"
)

for spec in "${SCALES[@]}"; do
  read -r users rate dur <<<"$spec"
  echo "=== scale: ${users} users (rate ${rate}/s, ${dur}) ==="
  locust -f scripts/load_test.py --host "$HOST" \
    --headless -u "$users" -r "$rate" -t "$dur" \
    --csv "${OUTDIR}/scale_${users}" --only-summary
  echo "--- cooldown 30s (let KEDA scale workers back toward zero) ---"
  sleep 30
done

echo "Done. Per-scale CSVs in ${OUTDIR}/ (scale_50*, scale_200*, scale_500*)."
echo "Next: poetry run python scripts/analyze_load_test.py"
