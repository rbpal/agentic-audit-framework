# AKS cluster + the shared static public IP.
#
# Free control-plane tier — $0, no uptime SLA (irrelevant for a 2-hr demo).
# Managed KEDA add-on scales the worker Deployment on Service Bus queue
# depth. The monitoring (Container Insights / oms_agent) block is
# deliberately OMITTED so container logs don't ingest into Log Analytics
# and blow the 1GB/day cap.

resource "azurerm_kubernetes_cluster" "this" {
  name                = "aks-${var.name_prefix}"
  location            = var.location
  resource_group_name = var.resource_group_name
  dns_prefix          = "aks-${var.name_prefix}"

  sku_tier = var.sku_tier

  # Workload Identity (OIDC federation) — lets worker pods assume a UAMI and
  # call Azure OpenAI via DefaultAzureCredential, no API key/secret. The
  # mutating webhook (installed by this flag) injects the projected token into
  # pods labelled azure.workload.identity/use=true. Harmless when unused
  # (mock-only runs), so it's always on.
  oidc_issuer_enabled       = var.workload_identity_enabled
  workload_identity_enabled = var.workload_identity_enabled

  default_node_pool {
    name                 = "system"
    vm_size              = var.node_vm_size
    auto_scaling_enabled = true
    min_count            = var.node_min_count
    max_count            = var.node_max_count
    vnet_subnet_id       = var.subnet_id
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin    = "azure" # Azure CNI → pods get real VNet IPs
    load_balancer_sku = "standard"
  }

  workload_autoscaler_profile {
    keda_enabled = var.keda_enabled
  }

  tags = var.tags
}

# ── Shared static public IP (LB + Front Door origin) ─────────────────
# Created in the AKS-owned node RG so the cluster's identity can attach it
# to the Standard LB (no extra role assignment). The K8s Service references
# this exact IP via `loadBalancerIP`, and Front Door uses it as its origin —
# so the origin is known at apply time (no kubectl-LB chicken-and-egg).
resource "azurerm_public_ip" "lb" {
  name                = "pip-${var.name_prefix}-lb"
  resource_group_name = azurerm_kubernetes_cluster.this.node_resource_group
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = var.tags
}
