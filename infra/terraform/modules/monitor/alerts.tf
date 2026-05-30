# Scheduled-query alert rules (Azure Monitor "log alerts", v2 API).
#
# Each rule runs its inline KQL on a fixed cadence over a trailing window
# and fires the shared Action Group (action_group.tf) when the result
# crosses a threshold. KQL + threshold + severity live together in one
# resource on purpose — the whole alert policy is diff-reviewable in a PR,
# not split across a query stored in the portal and a threshold in code.
#
# Two evaluation shapes are used:
#   * ratio/cost alerts — the KQL emits a single row with one measured
#     column; `metric_measure_column` + operator/threshold evaluate it.
#     Threshold stays a first-class, tunable alert property (visible in
#     `az monitor scheduled-query list`) rather than buried in a `where`.
#   * the floor alert — `time_aggregation_method = "Count"` fires on the
#     number of returned rows; no measure column.
#
# Dimension/event names mirror the kit's emitted telemetry and the three
# Workbooks (agent_success / cost_tokens / errors), so an operator who
# drilled from an alert into a Workbook sees the same fields:
#   llm.total_cost_usd · agent.status=="escalated_to_human" ·
#   customDimensions["layer"] · customDimensions["scenario_id"] ·
#   customEvents narrative_generated / narrative_hallucination_detected.
#
# Several rules depend on telemetry that only flows once chaos/hallucination
# scenarios run (Step 11) — they deploy correct-by-construction and stay
# quiet (the isnotempty/guard clauses yield no rows) until that data lands.

locals {
  # Single scope for every rule: the workspace-based App Insights resource.
  alert_scope = azurerm_application_insights.this.id
}

# ── 1. HighHallucinationRate ── Sev 1 (HIGH) ── rate > 1% over 10 min ──
# Hallucination events as a fraction of generated narratives. A spike here
# means the FactChecker is catching the narrative agent inventing facts.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "high_hallucination_rate" {
  name                = "HighHallucinationRate"
  resource_group_name = var.resource_group_name
  location            = var.location

  severity             = 1
  evaluation_frequency = "PT5M"
  window_duration      = "PT10M"
  scopes               = [local.alert_scope]
  description          = "Narrative hallucination rate exceeded 1% over the last 10 minutes."
  enabled              = true

  criteria {
    query                   = <<-KQL
      let total = toscalar(customEvents | where name == "narrative_generated" | count);
      customEvents
      | where name == "narrative_hallucination_detected"
      | summarize hallucinations = count()
      | extend hallucination_rate_pct = iff(total == 0, real(0), hallucinations * 100.0 / total)
      | project hallucination_rate_pct
    KQL
    time_aggregation_method = "Maximum"
    metric_measure_column   = "hallucination_rate_pct"
    operator                = "GreaterThan"
    threshold               = 1

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.this.id]
  }

  auto_mitigation_enabled = true
  tags                    = var.tags
}

# ── 2. CostPerRunSpike ── Sev 2 (MEDIUM) ── avg > $1.00 over 5 min ─────
# Mean LLM cost per agent run (llm.* spans summed per trace, averaged over
# the runs in the window). Catches a prompt change or model bump that
# silently tripled spend.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "cost_per_run_spike" {
  name                = "CostPerRunSpike"
  resource_group_name = var.resource_group_name
  location            = var.location

  severity             = 2
  evaluation_frequency = "PT5M"
  window_duration      = "PT5M"
  scopes               = [local.alert_scope]
  description          = "Average LLM cost per agent run exceeded $1.00 over the last 5 minutes."
  enabled              = true

  criteria {
    query                   = <<-KQL
      let llm_cost = dependencies
        | where name startswith "llm."
        | extend cost = todouble(customDimensions["llm.total_cost_usd"])
        | summarize total_cost = sum(cost) by operation_Id;
      let agent_runs = dependencies
        | where name startswith "agent."
        | distinct operation_Id;
      llm_cost
      | join kind=inner agent_runs on operation_Id
      | summarize avg_cost_per_run = avg(total_cost)
      | project avg_cost_per_run
    KQL
    time_aggregation_method = "Maximum"
    metric_measure_column   = "avg_cost_per_run"
    operator                = "GreaterThan"
    threshold               = 1.0

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.this.id]
  }

  auto_mitigation_enabled = true
  tags                    = var.tags
}

# ── 3. AgentEscalationRate ── Sev 2 (MEDIUM) ── rate > 30% over 15 min ─
# Share of agent runs that ended in escalation_to_human. A sustained
# climb means the supervisor is punting decisions it should be closing.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "agent_escalation_rate" {
  name                = "AgentEscalationRate"
  resource_group_name = var.resource_group_name
  location            = var.location

  severity             = 2
  evaluation_frequency = "PT5M"
  window_duration      = "PT15M"
  scopes               = [local.alert_scope]
  description          = "Agent escalation rate exceeded 30% over the last 15 minutes."
  enabled              = true

  criteria {
    query                   = <<-KQL
      dependencies
      | where name startswith "agent."
      | summarize escalations = countif(tostring(customDimensions["agent.status"]) == "escalated_to_human"),
                  total = count()
      | extend escalation_rate_pct = iff(total == 0, real(0), escalations * 100.0 / total)
      | project escalation_rate_pct
    KQL
    time_aggregation_method = "Maximum"
    metric_measure_column   = "escalation_rate_pct"
    operator                = "GreaterThan"
    threshold               = 30

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.this.id]
  }

  auto_mitigation_enabled = true
  tags                    = var.tags
}

# ── 4. LayerErrorBurst ── Sev 1 (HIGH) ── per-layer rate > 5% over 5 min
# Worst-layer exception rate: exceptions tagged with a `layer` dimension
# divided by total invocations carrying the same dimension. The isnotempty
# guards mean layers without the dimension simply don't contribute (no
# false fire) until the kit tags every layer span.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "layer_error_burst" {
  name                = "LayerErrorBurst"
  resource_group_name = var.resource_group_name
  location            = var.location

  severity             = 1
  evaluation_frequency = "PT5M"
  window_duration      = "PT5M"
  scopes               = [local.alert_scope]
  description          = "A pipeline layer exceeded a 5% exception rate over the last 5 minutes."
  enabled              = true

  criteria {
    query                   = <<-KQL
      let errors = exceptions
        | extend layer = tostring(customDimensions["layer"])
        | where isnotempty(layer)
        | summarize error_count = count() by layer;
      let invocations = union dependencies, requests
        | extend layer = tostring(customDimensions["layer"])
        | where isnotempty(layer)
        | summarize total = count() by layer;
      invocations
      | join kind=leftouter errors on layer
      | extend error_rate_pct = iff(total == 0, real(0), coalesce(error_count, 0) * 100.0 / total)
      | summarize max_error_rate_pct = max(error_rate_pct)
      | project max_error_rate_pct
    KQL
    time_aggregation_method = "Maximum"
    metric_measure_column   = "max_error_rate_pct"
    operator                = "GreaterThan"
    threshold               = 5

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.this.id]
  }

  auto_mitigation_enabled = true
  tags                    = var.tags
}

# ── 5. StructuralErrorFloor ── Sev 3 (LOW) ── count >= 1 over 1 hour ───
# A floor alert: any StructuralError from Layer 1 in the last hour is worth
# a low-priority heads-up. Count-aggregated — fires on row presence, so no
# measure column.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "structural_error_floor" {
  name                = "StructuralErrorFloor"
  resource_group_name = var.resource_group_name
  location            = var.location

  severity             = 3
  evaluation_frequency = "PT15M"
  window_duration      = "PT1H"
  scopes               = [local.alert_scope]
  description          = "At least one StructuralError was raised in the last hour."
  enabled              = true

  criteria {
    query                   = <<-KQL
      exceptions
      | where type endswith "StructuralError"
    KQL
    time_aggregation_method = "Count"
    operator                = "GreaterThanOrEqual"
    threshold               = 1

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.this.id]
  }

  auto_mitigation_enabled = true
  tags                    = var.tags
}
