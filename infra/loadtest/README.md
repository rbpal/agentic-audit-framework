# `infra/loadtest/` — transient AKS load-test stack (Step 11)

Isolated, **destroy-ASAP** stack: its own Terraform root + **local state**, a
dedicated RG (`rg-aaf-loadtest-dev`), and zero references to the durable
`infra/terraform/` stack. Deleting the RG cascades the AKS-owned `MC_…` node
RG. **Transient cost ~$1.50–2; forgotten ~$127/mo — tear it down the same
session.**

## Layout
Composed from small single-purpose modules; the root (`main.tf`) only creates
the RG and wires them together. All values come from `terraform.tfvars`
(auto-loaded — no `-var-file` needed).

```
infra/loadtest/
├── main.tf            # RG + module wiring
├── variables.tf       # root variables (all defaulted)
├── outputs.tf         # root outputs (names unchanged — README cmds still work)
├── terraform.tfvars   # every value in one place (non-secret; committed)
├── providers.tf · versions.tf
├── k8s/               # Deployments, Service, HPA, KEDA ScaledObject
└── modules/
    ├── network/       # VNet + AKS subnet (Azure CNI)
    ├── aks/           # cluster + shared static LB public IP
    ├── acr/           # registry + AcrPull for the kubelet identity
    ├── servicebus/    # namespace + queue (KEDA scale trigger)
    ├── workload_identity/  # UAMI + federated cred → keyless Azure OpenAI for workers
    └── frontdoor/     # optional global edge (count-gated on enable_front_door)
```
The one cross-stack object is `azurerm_role_assignment.workload_to_openai` at
the root — count-gated on `azure_openai_account_id`, so it exists only for a
REAL burst and `destroy` only ever removes the grant, never the OpenAI account.

## Prereqs
- `az login` on the **msn** tenant (`ad1de62c-…`), `ARM_SUBSCRIPTION_ID` set (sub `579425aa-…`).
- `kubectl`, `envsubst` (gettext), Docker not required (image builds in ACR).
- **Locust** for the load generator (`scripts/load_test.py`) — `pipx run locust` or `pip install locust`. Not bundled in the image; it runs from your machine against the Front Door / LB host.
- The durable-stack env for the REAL burst: `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `DATABRICKS_SQL_WAREHOUSE_ID`, and the `appi-aaf-dev` connection string. (The image bundles `ai_ops_kit` + `databricks-sql-connector` so the worker's REAL path runs; mock needs neither.)

## 1. Provision (~10 min)
```bash
terraform -chdir=infra/loadtest init
# REAL burst only — grant the worker's workload identity keyless access to the
# durable Azure OpenAI account (mock-only: skip this; role assignment = 0).
export TF_VAR_azure_openai_account_id=$(az cognitiveservices account show \
  -n aoai-aaf-rbpal-dev -g rg-agentic-audit-framework-dev --query id -o tsv)
terraform -chdir=infra/loadtest apply -auto-approve
# ⏰ The assistant arms a teardown reminder here.
export ACR_NAME=$(terraform -chdir=infra/loadtest output -raw acr_name)
export ACR_LOGIN_SERVER=$(terraform -chdir=infra/loadtest output -raw acr_login_server)
export LB_PIP_NAME="pip-aafload-lb"   # = pip-<name_prefix>-lb
export RG=$(terraform -chdir=infra/loadtest output -raw resource_group_name)
export FD=$(terraform -chdir=infra/loadtest output -raw front_door_endpoint)
export WORKLOAD_IDENTITY_CLIENT_ID=$(terraform -chdir=infra/loadtest output -raw workload_identity_client_id)
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

## 4. Apply manifests (envsubst fills image + IP name + WI client ID)
`serviceaccount` first — the worker pods reference it. It's keyless Azure
OpenAI for REAL mode (annotated with the UAMI client ID); mock pods ignore it.
```bash
for f in serviceaccount api-deployment api-service api-hpa worker-deployment worker-triggerauth worker-scaledobject; do
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
Pre-flight seatbelt — read the list before you destroy. It's scoped to this
stack's local state, so it can only ever name load-test resources (now
module-prefixed). If you ever see a `databricks`/`adls`/`monitor` resource
here, STOP — but you won't, they live in the other state file.
```bash
terraform -chdir=infra/loadtest state list
# expect ONLY:
#   azurerm_resource_group.loadtest
#   module.network.azurerm_virtual_network.this
#   module.network.azurerm_subnet.aks
#   module.aks.azurerm_kubernetes_cluster.this
#   module.aks.azurerm_public_ip.lb
#   module.acr.azurerm_container_registry.this
#   module.acr.azurerm_role_assignment.aks_acr_pull
#   module.servicebus.azurerm_servicebus_namespace.this
#   module.servicebus.azurerm_servicebus_queue.this
#   module.workload_identity.azurerm_user_assigned_identity.workload
#   module.workload_identity.azurerm_federated_identity_credential.workload
#   azurerm_role_assignment.workload_to_openai[0]   (ONLY if azure_openai_account_id was set — REAL burst)
#   module.frontdoor.azurerm_cdn_frontdoor_profile.this[0]   (+ endpoint/origin_group/origin/route)
```
Note `azurerm_role_assignment.workload_to_openai[0]` is the single cross-stack
object: `destroy` removes the role *grant* on the durable OpenAI account, never
the account itself. With `azure_openai_account_id` empty (mock-only) it isn't
in state at all.
Then tear down (any one; all leave the durable RG untouched):
```bash
terraform -chdir=infra/loadtest destroy -auto-approve
# or the panic buttons:
#   az group delete -n rg-aaf-loadtest-dev --yes --no-wait
#   Portal → delete rg-aaf-loadtest-dev   (cascades the MC_ node RG)

# Verify $0:
az resource list -g rg-aaf-loadtest-dev -o table        # expect empty / RG gone
az network public-ip list -o table | grep aafload || echo "no orphan IPs"
```
