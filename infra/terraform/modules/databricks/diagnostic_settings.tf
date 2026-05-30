# Databricks workspace → Log Analytics diagnostic forwarding.
#
# The "free win" of the observability stack: one resource flips on
# cross-stack correlation. With these logs landing in the same
# log-aaf-<env> workspace that backs appi-aaf-<env>, a single KQL query
# can join an audit-framework span (e.g. a Layer-1 gold-table read) to
# the Databricks job/cluster event it triggered — "the read that fired
# this job took X seconds" — without leaving the workspace.
#
# Category choice is deliberately FOCUSED to control log volume (and
# stay well under the workspace's free grant / daily cap):
#
#   unityCatalog — table/credential access on audit_<env> (the gold
#                  reads/writes the framework actually performs)
#   clusters     — cluster create/start/terminate (capacity + cold-start
#                  latency that explains slow first reads)
#   jobs         — job run lifecycle (what the pipeline scheduled)
#   notebook     — notebook command execution (interactive/ad-hoc work)
#
# Noisy categories (dbfs, accounts, and the long tail of ML/SQL/feature
# categories) are intentionally OMITTED — they produce gigabytes of
# detail no current dashboard consumes. Adding one is a one-line change
# if a future Workbook needs it; tuning here is operational, not
# architectural.
#
# count-guarded on log_analytics_workspace_id so the module stays usable
# standalone — diagnostic forwarding is opt-in, enabled by the root
# composition passing the monitor module's workspace ID.

resource "azurerm_monitor_diagnostic_setting" "to_log_analytics" {
  count = var.log_analytics_workspace_id == null ? 0 : 1

  name                       = "diag-${var.workspace_name}-to-log-analytics"
  target_resource_id         = azurerm_databricks_workspace.this.id
  log_analytics_workspace_id = var.log_analytics_workspace_id

  enabled_log {
    category = "unityCatalog"
  }

  enabled_log {
    category = "clusters"
  }

  enabled_log {
    category = "jobs"
  }

  enabled_log {
    category = "notebook"
  }
}
