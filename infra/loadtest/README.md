# `infra/loadtest/` — transient AKS load-test stack (Step 11)

Isolated, **destroy-ASAP** stack: its own Terraform root + **local state**, a
dedicated RG (`rg-aaf-loadtest-dev`), and zero references to the durable
`infra/terraform/` stack. Deleting the RG cascades the AKS-owned `MC_…` node
RG. **Transient cost ~$1.50–2; forgotten ~$127/mo — tear it down the same
session.**

## Prereqs
- `az login` on the **msn** tenant (`ad1de62c-…`), `ARM_SUBSCRIPTION_ID` set (sub `579425aa-…`).
- `kubectl`, `envsubst` (gettext), Docker not required (image builds in ACR).
- The durable-stack env for the REAL burst: `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `DATABRICKS_SQL_WAREHOUSE_ID`, and the `appi-aaf-dev` connection string.

## 1. Provision (~10 min)
```bash
terraform -chdir=infra/loadtest init
terraform -chdir=infra/loadtest apply -auto-approve
# ⏰ The assistant arms a teardown reminder here.
export ACR_NAME=$(terraform -chdir=infra/loadtest output -raw acr_name)
export ACR_LOGIN_SERVER=$(terraform -chdir=infra/loadtest output -raw acr_login_server)
export LB_PIP_NAME="pip-aafload-lb"   # = pip-<name_prefix>-lb
export RG=$(terraform -chdir=infra/loadtest output -raw resource_group_name)
export FD=$(terraform -chdir=infra/loadtest output -raw front_door_endpoint)
```

## 2. Build + push the image (in ACR — no local Docker)
```bash
az acr build -r "$ACR_NAME" -t audit-api:load .
```

## 3. Cluster creds + the secret
```bash
az aks get-credentials -g "$RG" -n "$(terraform -chdir=infra/loadtest output -raw aks_name)"

kubectl create secret generic aaf-loadtest-secrets \
  --from-literal=SERVICEBUS_CONNECTION_STRING="$(terraform -chdir=infra/loadtest output -raw servicebus_connection_string)" \
  --from-literal=AZURE_OPENAI_ENDPOINT="https://aoai-aaf-rbpal-dev.openai.azure.com/" \
  --from-literal=DATABRICKS_HOST="$DATABRICKS_HOST" \
  --from-literal=DATABRICKS_TOKEN="$DATABRICKS_TOKEN" \
  --from-literal=DATABRICKS_SQL_WAREHOUSE_ID="$DATABRICKS_SQL_WAREHOUSE_ID" \
  --from-literal=APPLICATIONINSIGHTS_CONNECTION_STRING="$APPLICATIONINSIGHTS_CONNECTION_STRING"
```

## 4. Apply manifests (envsubst fills image + IP name)
```bash
for f in api-deployment api-service api-hpa worker-deployment worker-triggerauth worker-scaledobject; do
  envsubst < "infra/loadtest/k8s/$f.yaml" | kubectl apply -f -
done
kubectl get svc audit-api -w     # wait for EXTERNAL-IP = the static IP
```

## 5. Verify serving (via Front Door, or the LB IP if FD disabled)
```bash
curl -s -X POST "https://$FD/run" -H 'content-type: application/json' \
  -d '{"scenario_id":"dc9_Q2"}' ; echo
# expect: 202 {"job_id":"…","scenario_id":"dc9_Q2","status":"queued"}
```

## 6. Run the load test (mock scaling, then real burst) — see scripts/run_load_tests.sh
Watch scaling in separate panes:
```bash
kubectl get hpa -w
kubectl get pods -w        # worker pods scale 0→N on queue depth (KEDA)
kubectl get nodes -w       # cluster-autoscaler may add a 2nd node
```
Real burst:
```bash
kubectl set env deploy/audit-worker AAF_MOCK_BACKENDS=0
# enqueue ~10 real jobs (Locust short burst or curl loop), then revert.
```

## 7. TEARDOWN — same session (any one; all leave the durable RG untouched)
```bash
terraform -chdir=infra/loadtest destroy -auto-approve
# or the panic buttons:
#   az group delete -n rg-aaf-loadtest-dev --yes --no-wait
#   Portal → delete rg-aaf-loadtest-dev   (cascades the MC_ node RG)

# Verify $0:
az resource list -g rg-aaf-loadtest-dev -o table        # expect empty / RG gone
az network public-ip list -o table | grep aafload || echo "no orphan IPs"
```
