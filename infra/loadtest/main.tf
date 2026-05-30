resource "azurerm_resource_group" "loadtest" {
  name     = var.resource_group_name
  location = var.location
  tags     = var.tags
}

# ── Network (Azure CNI) ──────────────────────────────────────────────
# Pods get real VNet IPs from this subnet (Azure CNI). /22 = 1024 IPs,
# ample for a 1-3 node, max-pods-default cluster.
resource "azurerm_virtual_network" "this" {
  name                = "vnet-${var.name_prefix}"
  location            = azurerm_resource_group.loadtest.location
  resource_group_name = azurerm_resource_group.loadtest.name
  address_space       = ["10.224.0.0/16"]
  tags                = var.tags
}

resource "azurerm_subnet" "aks" {
  name                 = "snet-aks"
  resource_group_name  = azurerm_resource_group.loadtest.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = ["10.224.0.0/22"]
}

# ── AKS ──────────────────────────────────────────────────────────────
resource "azurerm_kubernetes_cluster" "this" {
  name                = "aks-${var.name_prefix}"
  location            = azurerm_resource_group.loadtest.location
  resource_group_name = azurerm_resource_group.loadtest.name
  dns_prefix          = "aks-${var.name_prefix}"

  # Free control-plane tier — $0, no uptime SLA (irrelevant for a 2-hr demo).
  sku_tier = "Free"

  default_node_pool {
    name                 = "system"
    vm_size              = var.node_vm_size
    auto_scaling_enabled = true
    min_count            = var.node_min_count
    max_count            = var.node_max_count
    vnet_subnet_id       = azurerm_subnet.aks.id
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin    = "azure" # Azure CNI → pods get real VNet IPs
    load_balancer_sku = "standard"
  }

  # Managed KEDA add-on — scales the worker Deployment on Service Bus
  # queue depth. NOTE: the monitoring (Container Insights / oms_agent)
  # block is deliberately OMITTED so container logs don't ingest into
  # Log Analytics and blow the 1GB/day cap.
  workload_autoscaler_profile {
    keda_enabled = true
  }

  tags = var.tags
}

# ── Container registry ───────────────────────────────────────────────
resource "azurerm_container_registry" "this" {
  name                = "acr${var.name_prefix}"
  resource_group_name = azurerm_resource_group.loadtest.name
  location            = azurerm_resource_group.loadtest.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = var.tags
}

# Let the cluster pull from the registry (kubelet identity → AcrPull).
resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
}

# ── Shared static public IP (LB + Front Door origin) ─────────────────
# Created in the AKS-owned node RG so the cluster's identity can attach it
# to the Standard LB (no extra role assignment). The K8s Service references
# this exact IP via `loadBalancerIP`, and Front Door uses it as its origin —
# so the origin is known at apply time (no kubectl-LB chicken-and-egg).
resource "azurerm_public_ip" "lb" {
  name                = "pip-${var.name_prefix}-lb"
  resource_group_name = azurerm_kubernetes_cluster.this.node_resource_group
  location            = azurerm_resource_group.loadtest.location
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = var.tags
}
