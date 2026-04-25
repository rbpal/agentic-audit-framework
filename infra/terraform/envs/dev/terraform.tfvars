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
