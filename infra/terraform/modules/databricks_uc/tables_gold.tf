# Gold tables (step_03_task_08).
#
# Curated, aggregated, ready for direct consumption by the agent
# loop, eval harness, dashboards, and partner-review reports.
#
# Four tables in audit_dev.gold:
#   - agent_claims    : the agent's pass/fail/inconclusive claims per attribute per quarter
#                       — defensible because each row has source_evidence_ids tracing back to silver.evidence
#   - audit_findings  : final findings ready for partner review (severity, recommendation, status)
#   - eval_outcomes   : eval-suite results comparing actual agent claims vs gold answers
#   - cost_telemetry  : per-agent-run token spend, latency, model version

resource "databricks_sql_table" "gold_agent_claims" {
  catalog_name       = databricks_catalog.this.name
  schema_name        = databricks_schema.this["gold"].name
  name               = "agent_claims"
  table_type         = "MANAGED"
  data_source_format = "DELTA"

  comment = "Agent's pass/fail/inconclusive claims per (control, attribute, quarter). Each row carries source_evidence_ids — without that backreference, gold becomes opinion-without-evidence and the SOX narrative collapses. step_03_task_08."

  column {
    name    = "claim_id"
    type    = "bigint"
    comment = "Primary key. Auto-generated."
  }
  column {
    name    = "engagement_id"
    type    = "string"
    comment = "Engagement identifier — scopes the claim to a specific audit engagement."
  }
  column {
    name    = "control_id"
    type    = "string"
    comment = "Control identifier (e.g., 'DC-2'). FK-style reference to silver.controls."
  }
  column {
    name     = "attribute_id"
    type     = "string"
    nullable = true
    comment  = "Optional attribute identifier (e.g., 'DC-2.A'). NULL when the claim is at the control level rather than per-attribute."
  }
  column {
    name    = "quarter"
    type    = "string"
    comment = "Quarter the claim applies to. Partition column — most queries scope by quarter."
  }
  column {
    name    = "claim_status"
    type    = "string"
    comment = "'pass' / 'fail' / 'inconclusive'. The agent's verdict."
  }
  column {
    name    = "confidence"
    type    = "double"
    comment = "Agent's confidence in the verdict (0.0–1.0). Used by the eval harness as a calibration signal."
  }
  column {
    name    = "evidence_summary"
    type    = "string"
    comment = "Free-text rationale the agent produced. Reads as the 'why this verdict' explanation. PII-mask candidate."
  }
  column {
    name    = "source_evidence_ids"
    type    = "array<bigint>"
    comment = "Foreign-key-style array of silver.evidence.evidence_id values that the agent consulted. THE LINEAGE — without this the claim is unauditable."
  }
  column {
    name    = "agent_run_id"
    type    = "string"
    comment = "Identifier of the agent run that produced this claim. Joins to gold.cost_telemetry.agent_run_id."
  }
  column {
    name    = "model_version"
    type    = "string"
    comment = "Pinned model version used (e.g., 'gpt-4o:2024-11-20'). Required for SOX reproducibility."
  }
  column {
    name    = "created_at"
    type    = "timestamp"
    comment = "When the claim was emitted by the agent run."
  }
}

resource "databricks_sql_table" "gold_audit_findings" {
  catalog_name       = databricks_catalog.this.name
  schema_name        = databricks_schema.this["gold"].name
  name               = "audit_findings"
  table_type         = "MANAGED"
  data_source_format = "DELTA"

  comment = "Final findings ready for partner review. Derived from gold.agent_claims (with confidence threshold) plus silver.cross_file_validations failures. step_03_task_08."

  column {
    name    = "finding_id"
    type    = "bigint"
    comment = "Primary key."
  }
  column {
    name    = "engagement_id"
    type    = "string"
    comment = "Engagement identifier."
  }
  column {
    name    = "control_id"
    type    = "string"
    comment = "Control the finding applies to."
  }
  column {
    name    = "quarter"
    type    = "string"
    comment = "Quarter — partition column."
  }
  column {
    name    = "severity"
    type    = "string"
    comment = "'low' / 'medium' / 'high' / 'critical'. Drives partner-review prioritisation + escalation rules."
  }
  column {
    name    = "summary"
    type    = "string"
    comment = "One-line description of the finding."
  }
  column {
    name    = "recommendation"
    type    = "string"
    comment = "Recommended remediation action."
  }
  column {
    name    = "status"
    type    = "string"
    comment = "'open' / 'in_review' / 'resolved' / 'accepted_risk'. Workflow state."
  }
  column {
    name    = "claim_id"
    type    = "bigint"
    comment = "Optional FK-style reference to gold.agent_claims.claim_id when the finding originates from a failed agent claim."
  }
  column {
    name    = "validation_id"
    type    = "bigint"
    comment = "Optional FK-style reference to silver.cross_file_validations.validation_id when the finding originates from a cross-file mismatch."
  }
  column {
    name    = "created_at"
    type    = "timestamp"
    comment = "When the finding was logged."
  }
  column {
    name    = "updated_at"
    type    = "timestamp"
    comment = "Last status update."
  }
}

resource "databricks_sql_table" "gold_eval_outcomes" {
  catalog_name       = databricks_catalog.this.name
  schema_name        = databricks_schema.this["gold"].name
  name               = "eval_outcomes"
  table_type         = "MANAGED"
  data_source_format = "DELTA"

  comment = "Eval-suite outcomes — compares actual agent claims (gold.agent_claims) against gold answers from the engagement TOC files (bronze.tocs_raw → silver). One row per (agent_run_id, control_id, attribute_id, quarter). step_03_task_08."

  column {
    name    = "eval_id"
    type    = "bigint"
    comment = "Primary key."
  }
  column {
    name    = "agent_run_id"
    type    = "string"
    comment = "Agent run being evaluated. Joins to gold.cost_telemetry."
  }
  column {
    name    = "engagement_id"
    type    = "string"
    comment = "Engagement identifier."
  }
  column {
    name    = "control_id"
    type    = "string"
    comment = "Control under evaluation."
  }
  column {
    name     = "attribute_id"
    type     = "string"
    nullable = true
    comment  = "Optional attribute identifier."
  }
  column {
    name    = "quarter"
    type    = "string"
    comment = "Quarter being evaluated."
  }
  column {
    name    = "expected_status"
    type    = "string"
    comment = "Gold-answer verdict from the engagement TOC ('pass' / 'fail' / 'inconclusive')."
  }
  column {
    name    = "actual_status"
    type    = "string"
    comment = "Agent's claim_status for this (control, attribute, quarter)."
  }
  column {
    name    = "match"
    type    = "boolean"
    comment = "Boolean: did actual_status equal expected_status?"
  }
  column {
    name    = "score"
    type    = "double"
    comment = "Per-row evaluation score (e.g., 1.0 for exact match, partial credit for close-but-wrong). Aggregated for run-level metrics."
  }
  column {
    name    = "created_at"
    type    = "timestamp"
    comment = "When the eval row was computed."
  }
}

resource "databricks_sql_table" "gold_cost_telemetry" {
  catalog_name       = databricks_catalog.this.name
  schema_name        = databricks_schema.this["gold"].name
  name               = "cost_telemetry"
  table_type         = "MANAGED"
  data_source_format = "DELTA"

  comment = "Per-agent-run token spend, latency, model version. One row per agent_run_id — used for cost-governance dashboards + runaway-loop detection. step_03_task_08."

  column {
    name    = "agent_run_id"
    type    = "string"
    comment = "Agent run identifier. Joins to gold.agent_claims and gold.eval_outcomes."
  }
  column {
    name    = "input_tokens"
    type    = "bigint"
    comment = "Total input tokens (prompt) consumed in this run."
  }
  column {
    name    = "output_tokens"
    type    = "bigint"
    comment = "Total output tokens (completion) generated in this run."
  }
  column {
    name    = "total_tokens"
    type    = "bigint"
    comment = "input_tokens + output_tokens. Denormalised for query speed."
  }
  column {
    name    = "latency_ms"
    type    = "bigint"
    comment = "Total wall-clock latency in milliseconds for the agent run end-to-end."
  }
  column {
    name    = "cost_usd"
    type    = "double"
    comment = "Estimated cost in USD based on the model's pricing at the time of the run."
  }
  column {
    name    = "model_version"
    type    = "string"
    comment = "Model version used (e.g., 'gpt-4o:2024-11-20'). Pinned per project policy."
  }
  column {
    name    = "started_at"
    type    = "timestamp"
    comment = "When the agent run started."
  }
  column {
    name    = "completed_at"
    type    = "timestamp"
    comment = "When the agent run finished. Difference vs started_at = wall-clock latency."
  }
}
