# Action Group — the notification fan-out target shared by every alert.
#
# An Action Group is the indirection layer between "an alert fired" and
# "who hears about it". Each scheduled-query rule in alerts.tf references
# this single group, so swapping the operator (or, in production, adding
# a PagerDuty/OpsGenie webhook) is a one-resource edit that re-points all
# five alerts at once — no per-alert receiver duplication.
#
# Action Groups are global resources: no `location` argument, and the
# short_name (<= 12 chars) is what prefixes the notification subject line.
#
# Dev ships a single email receiver (free, sufficient for a portfolio
# stack). Production would keep the email and add a webhook receiver to
# bridge into an on-call rotation.

resource "azurerm_monitor_action_group" "this" {
  name                = var.action_group_name
  resource_group_name = var.resource_group_name
  short_name          = var.action_group_short_name

  email_receiver {
    name          = "operator-email"
    email_address = var.operator_email
    # Common alert schema normalises the payload across alert types so a
    # downstream webhook/Logic App can parse one shape. Harmless for plain
    # email and required the moment a webhook receiver is added.
    use_common_alert_schema = true
  }

  tags = var.tags
}
