# Azure Front Door (Standard) — global edge in front of the AKS load
# balancer. Optional (var.enabled): the LB public IP alone serves a
# single-region test fine, but Front Door is kept for resume breadth.
#
# Origin = the shared static public IP (aks module). Because it's a raw IP (no
# matching TLS cert), certificate_name_check_enabled must be false and Front
# Door forwards to it over plain HTTP (the app serves HTTP in-cluster; Front
# Door terminates client TLS at the edge with its default *.azurefd.net cert).
#
# Every resource here is count-gated on var.enabled so the whole module
# collapses to zero resources when Front Door is disabled.

resource "azurerm_cdn_frontdoor_profile" "this" {
  count               = var.enabled ? 1 : 0
  name                = "afd-${var.name_prefix}"
  resource_group_name = var.resource_group_name
  sku_name            = "Standard_AzureFrontDoor"
  tags                = var.tags
}

resource "azurerm_cdn_frontdoor_endpoint" "this" {
  count                    = var.enabled ? 1 : 0
  name                     = "fde-${var.name_prefix}"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.this[0].id
  tags                     = var.tags
}

resource "azurerm_cdn_frontdoor_origin_group" "this" {
  count                    = var.enabled ? 1 : 0
  name                     = "og-aks"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.this[0].id

  load_balancing {
    sample_size                 = 4
    successful_samples_required = 2
  }

  health_probe {
    path                = var.health_probe_path
    protocol            = "Http"
    request_type        = "GET"
    interval_in_seconds = 30
  }
}

resource "azurerm_cdn_frontdoor_origin" "aks_lb" {
  count                          = var.enabled ? 1 : 0
  name                           = "origin-aks-lb"
  cdn_frontdoor_origin_group_id  = azurerm_cdn_frontdoor_origin_group.this[0].id
  enabled                        = true
  host_name                      = var.origin_ip
  origin_host_header             = var.origin_ip
  http_port                      = 80
  https_port                     = 443
  priority                       = 1
  weight                         = 1000
  certificate_name_check_enabled = false # raw-IP origin, no cert to validate
}

resource "azurerm_cdn_frontdoor_route" "this" {
  count                         = var.enabled ? 1 : 0
  name                          = "route-run"
  cdn_frontdoor_endpoint_id     = azurerm_cdn_frontdoor_endpoint.this[0].id
  cdn_frontdoor_origin_group_id = azurerm_cdn_frontdoor_origin_group.this[0].id
  cdn_frontdoor_origin_ids      = [azurerm_cdn_frontdoor_origin.aks_lb[0].id]

  supported_protocols    = ["Http", "Https"]
  patterns_to_match      = ["/*"]
  forwarding_protocol    = "HttpOnly" # edge → origin over HTTP (app serves HTTP)
  https_redirect_enabled = false
  link_to_default_domain = true
}
