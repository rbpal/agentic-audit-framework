# Dev environment values for the single-root Terraform composition.
#
# Apply pattern (Task 09):
#   terraform -chdir=infra/terraform init  \
#       -backend-config=envs/dev/backend.conf
#   terraform -chdir=infra/terraform plan   \
#       -var-file=envs/dev/terraform.tfvars
#   terraform -chdir=infra/terraform apply  \
#       -var-file=envs/dev/terraform.tfvars
#
# Safe to commit — contains no secrets. Subscription / tenant IDs
# are read from the operator's ARM_SUBSCRIPTION_ID env var, never
# from this file (see step_02_terraform_iac.md §4.2).

environment = "dev"
location    = "eastus2"
owner       = "rbpal"
name_suffix = "rbpal"

# OpenAI module
openai_account_name       = "aoai-aaf-dev"
openai_model_version      = "2024-11-20"
openai_model_capacity_tpm = 10

# Local-dev developers granted `Cognitive Services OpenAI User` role
# on the OpenAI account. Adding a contributor: append their AAD
# object-id (`az ad signed-in-user show --query id -o tsv`) to this
# list and `terraform apply`. Removing access: drop the entry +
# `terraform apply` (Terraform issues the role-assignment delete).
openai_data_plane_user_principal_ids = [
  # rajendra_b_pal@msn.com (External / Rajendra B Pal). Originally
  # granted manually 2026-05-05 via `az role assignment create` to
  # unblock step_05_task_03's integration smoke test; brought under
  # Terraform management 2026-05-06.
  "4c92c956-f8fe-4cdc-91f8-bade7f42d7d1",
]

# ADLS module — dev uses LRS (locally redundant) instead of the module's
# GRS default. Dev data is reproducible (re-ingest bronze / re-run
# sweeps), so paying ~2x for geo-redundancy isn't justified on a
# credit-capped subscription. Prod tfvars should set GRS. This is an
# in-place redundancy change — no storage-account recreate, no data loss.
adls_replication_type = "LRS"
