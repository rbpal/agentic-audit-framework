# Provider configurations.
#
# Subscription targeting: providers read ARM_SUBSCRIPTION_ID from the
# environment (set in operator shell config, never committed). Auth uses
# the active Azure CLI token by default — operators must `az login`
# before running terraform commands.
#
# The `databricks` provider is intentionally NOT configured here: it
# cannot initialise without a workspace URL or Azure resource ID, which
# don't exist until Task 05 creates the workspace. Task 05 adds the
# provider config.

provider "azurerm" {
  features {}
}

provider "azapi" {
  # azapi inherits ARM_SUBSCRIPTION_ID + az-CLI auth automatically.
  # No explicit fields needed for local dev.
}
