# Azure OpenAI module.
#
# Creates a Cognitive Services account (kind = "OpenAI") with a single
# GPT-4o deployment and a system-assigned managed identity. The MSI is
# the identity AOAI presents when reaching out to other Azure resources
# (e.g. customer-managed keys in Key Vault, content moderation against
# blob storage). Inbound auth — i.e. how the agent loop calls AOAI — is
# wired in Task 06 via role assignments on the calling principal.
#
# Custom subdomain (var.custom_subdomain_name) is mandatory for Azure AD
# / managed-identity authentication — the API-key-based endpoint uses
# the regional URL, but the MSI flow requires the per-account DNS name.
# The subdomain is globally unique across Azure's openai.azure.com zone.

resource "azurerm_cognitive_account" "this" {
  name                = var.account_name
  location            = var.location
  resource_group_name = var.resource_group_name

  kind     = "OpenAI"
  sku_name = "S0"

  custom_subdomain_name = var.custom_subdomain_name

  identity {
    type = "SystemAssigned"
  }

  public_network_access_enabled = var.public_network_access_enabled

  tags = var.tags
}

resource "azurerm_cognitive_deployment" "gpt4o" {
  name                 = var.deployment_name
  cognitive_account_id = azurerm_cognitive_account.this.id

  model {
    format  = "OpenAI"
    name    = "gpt-4o"
    version = var.model_version
  }

  sku {
    name     = "Standard"
    capacity = var.model_capacity_tpm
  }
}
