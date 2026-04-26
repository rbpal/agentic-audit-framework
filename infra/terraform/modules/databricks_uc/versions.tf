# Required for non-hashicorp providers to resolve correctly inside a
# child module — Terraform won't auto-inherit the source address from
# the root, so we declare it here too. Version range matches the root
# pin in infra/terraform/versions.tf.
terraform {
  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.50"
    }
  }
}
