# Isolated, transient load-test stack (Step 11). SEPARATE root + state from
# infra/terraform/ so `terraform destroy` here is atomic and cannot touch the
# durable Databricks/ADLS/monitor stack.
#
# State is LOCAL on purpose — this is throwaway infra with a ≤2-hour lifetime;
# a remote backend (storage account + locking) would be overkill and add a
# resource that itself needs cleanup.

terraform {
  required_version = ">= 1.6"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}
