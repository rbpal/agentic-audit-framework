# Gold tables (step_03_task_08, extended in step_05_task_06,
# step_06_task_04, step_07_task_00).
#
# Curated, aggregated, ready for direct consumption by the agent
# loop, eval harness, dashboards, and partner-review reports.
#
# Seven tables in audit_dev.gold:
#   - agent_claims      : the agent's pass/fail/inconclusive claims per attribute per quarter
#                         — defensible because each row has source_evidence_ids tracing back to silver.evidence
#   - audit_findings    : final findings ready for partner review (severity, recommendation, status)
#   - eval_outcomes     : eval-suite results comparing actual agent claims vs gold answers
#   - cost_telemetry    : per-agent-run token spend, latency, model version
#   - narratives        : Layer 2 LLM-generated grounded narratives + inlined fact-check verdict
#                         (step_05_task_06). Keyed by (engagement, control, quarter, attribute, prompt_version)
#                         so prompt A/B versions land as parallel rows, not overwrites.
#   - judge_outcomes    : Step 6 LLM-as-judge verdicts over Layer 2 narratives. Append-only per judge_run_id.
#                         (step_06_task_04). Distinct from eval_outcomes (Layer-1 deterministic comparison).
#   - layer3_decisions  : Step 7 supervisor write target — one row per Layer-3 multi-agent investigation.
#                         Holds verdict + reasoning chain + full tool trace (JSON). Append-only per
#                         investigation_run_id. (step_07_task_00).
#
# Append-only invariant: when adding columns to any databricks_sql_table
# in this file, APPEND to the end of the column block. Mid-block insertion
# triggers a positional rename cascade in the provider — see TECH_DEBT.md
# "databricks_sql_table provider tracks columns positionally" for the
# 2026-05-12 incident.

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

resource "databricks_sql_table" "gold_narratives" {
  catalog_name       = databricks_catalog.this.name
  schema_name        = databricks_schema.this["gold"].name
  name               = "narratives"
  table_type         = "MANAGED"
  data_source_format = "DELTA"

  comment = "Layer 2 LLM-generated grounded narratives + inlined fact-check verdict. One row per (engagement, control, quarter, attribute, prompt_version) — prompt versions land as parallel rows, not overwrites, so v1.0 and v1.1 outputs coexist for A/B comparison. Written by GoldNarrativeWriter via MERGE INTO. step_05_task_06."

  # Composite primary key — Delta does not enforce, but the writer's
  # MERGE INTO matches on these five columns:
  #   (engagement_id, control_id, quarter, attribute_id, prompt_version)
  # See privateDocs/step_05_layer2_narrative.md Q17.

  column {
    name    = "engagement_id"
    type    = "string"
    comment = "Engagement identifier — first key column. Multi-tenant scope; never collapse across tenants."
  }
  column {
    name    = "control_id"
    type    = "string"
    comment = "SOX control being narrated (e.g., 'DC-2', 'DC-9')."
  }
  column {
    name    = "quarter"
    type    = "string"
    comment = "Audit period (e.g., 'Q1', 'Q2'). Same control narrates independently per quarter."
  }
  column {
    name    = "attribute_id"
    type    = "string"
    comment = "Sub-attribute within the control (e.g., 'A', 'C', 'D' for DC-2; 'A', 'B', 'C', 'E', 'F' for DC-9). Layer 3 attributes (DC-2.B, DC-9.D) deliberately do NOT appear here."
  }
  column {
    name    = "prompt_version"
    type    = "string"
    comment = "Git-pinned prompt template version (e.g., 'v1.0'). Including this in the key means re-running with v1.1 inserts a parallel row instead of overwriting v1.0 — required for A/B comparison and task_08 baseline tracking."
  }
  column {
    name    = "source_evidence_id"
    type    = "string"
    comment = "Lineage back to the silver.evidence row that fed NarrativeGenerator.generate(). Natural-key form '{engagement}|{control}|{quarter}|{attribute}'."
  }
  column {
    name    = "narrative_text"
    type    = "string"
    comment = "The LLM output verbatim — the auditor-facing 150-word grounded narrative. THE primary artefact of this table."
  }
  column {
    name    = "cited_fields"
    type    = "array<string>"
    comment = "LLM self-report of which evidence fields it cited. Distinct from fact_check_issues — this is what the LLM CLAIMS it cited; the fact-checker verifies what it actually cited."
  }
  column {
    name    = "word_count"
    type    = "int"
    comment = "Word count of narrative_text. Always <= 150 by post-process contract; pydantic-validated at generation time."
  }
  column {
    name    = "model_deployment"
    type    = "string"
    comment = "Azure OpenAI deployment name (e.g., 'gpt-4o'). Pinned per generator instance. Required for SOX reproducibility."
  }
  column {
    name    = "generation_run_id"
    type    = "string"
    comment = "Per-call run identifier. Joins to gold.cost_telemetry to answer 'what did this narrative cost to produce?'"
  }
  column {
    name    = "generated_at"
    type    = "timestamp"
    comment = "UTC timestamp at LLM call time."
  }
  column {
    name    = "fact_check_passed"
    type    = "boolean"
    comment = "FactChecker.check() verdict: true = every numeric and entity in narrative_text appears in source evidence; false = at least one ungrounded token."
  }
  column {
    name    = "fact_check_issues"
    type    = "array<string>"
    comment = "When fact_check_passed=false, the human-readable list of ungrounded tokens (e.g., \"numeric not in evidence: '$2,500'\"). Empty array when passed=true."
  }
  # narrative_call_id appended at END of block — Step 5 follow-up #5.
  # The databricks_sql_table provider tracks columns POSITIONALLY;
  # inserting in the middle triggers a destructive rename cascade.
  # Always append. See TECH_DEBT.md and the 2026-05-12 incident notes.
  column {
    name     = "narrative_call_id"
    type     = "string"
    nullable = true
    comment  = "Per-CALL ULID — fresh per NarrativeGenerator.generate() invocation. Joins to gold.judge_outcomes.narrative_run_id (also misnamed — Step 5 follow-up #5 tracker). NULL on historical v1.0 rows written before this column existed; all v1.1+ sweeps populate. Lets divergence queries join 1:1 between narrative and judgment without the composite-key workaround used today in scripts/divergence_summary.sql Q2."
  }
}

resource "databricks_sql_table" "gold_judge_outcomes" {
  catalog_name       = databricks_catalog.this.name
  schema_name        = databricks_schema.this["gold"].name
  name               = "judge_outcomes"
  table_type         = "MANAGED"
  data_source_format = "DELTA"

  comment = "Step 6 LLM-as-judge verdicts over Layer 2 narratives. One row per (judge_run_id, narrative_run_id). Distinct from gold.eval_outcomes (which is shaped for Layer-1 deterministic claim/answer comparison) — see privateDocs/step_06_eval_harness.md Design rationale § A. step_06_task_04."

  column {
    name    = "judge_run_id"
    type    = "string"
    comment = "Per-sweep run identifier for the judge sweep itself. Joins to gold.cost_telemetry. Distinct from the Layer-2 generator's agent_run_id and from any Step 7 supervisor run id."
  }
  column {
    name    = "narrative_run_id"
    type    = "string"
    comment = "Joins back to gold.narratives.generation_run_id — the narrative this verdict was rendered against."
  }
  column {
    name    = "engagement_id"
    type    = "string"
    comment = "Engagement identifier. Denormalised from gold.narratives so Step 7's supervisor can filter by tenant scope without joining."
  }
  column {
    name    = "control_id"
    type    = "string"
    comment = "SOX control under judgment (e.g., 'DC-2', 'DC-9'). Denormalised."
  }
  column {
    name    = "attribute_id"
    type    = "string"
    comment = "Sub-attribute within the control (A-F). Denormalised."
  }
  column {
    name    = "quarter"
    type    = "string"
    comment = "Audit period (e.g., 'Q1'). Denormalised."
  }
  column {
    name    = "judge_verdict"
    type    = "string"
    comment = "LLM-as-judge verdict: 'pass' | 'fail' | 'uncertain'. Distinct from fact_check_verdict — judge catches semantic hallucination; FactChecker catches numeric/entity fabrication."
  }
  column {
    name    = "judge_confidence"
    type    = "double"
    comment = "Self-reported judge confidence in [0, 1]. Step 7's gate consults (verdict, confidence) jointly."
  }
  column {
    name    = "judge_reasoning"
    type    = "string"
    comment = "Human-readable rationale for the verdict. NOT used for control flow — Step 7's gate must not regex this string. Status-vs-fallback distinction lives in judge_status."
  }
  column {
    name    = "cited_evidence_fields"
    type    = "array<string>"
    comment = "Evidence fields the judge anchored on. Decision Rule 1: pass/fail must be non-empty; uncertain may be empty. Surfaces in human-reviewer escalation."
  }
  column {
    name    = "judge_status"
    type    = "string"
    comment = "Operational status enum: 'ok' (genuine verdict) | 'parse_failure' | 'validation_failure' | 'empty_content' (LLM-side failure routed through the uncertain fallback). Lets the gate distinguish a genuine 'evidence is silent' uncertain from 'the LLM crashed twice.'"
  }
  column {
    name    = "gold_expected_verdict"
    type    = "string"
    comment = "Ground-truth expected verdict from eval/gold_scenarios/tocs/*.json → expected_attribute_results['<control>.<attribute>']. Materialised here so 'did we get it right' rollups are a single-table query."
  }
  column {
    name    = "fact_check_verdict"
    type    = "string"
    comment = "Denormalised from gold.narratives.fact_check_passed (true→'pass', false→'fail') for divergence queries vs judge_verdict. FactChecker is deterministic, so no 'uncertain' case."
  }
  column {
    name    = "prompt_version"
    type    = "string"
    comment = "Git-pinned judge prompt template version (e.g., 'judge_v1.0'). SOX reproducibility — re-running with v1.1 inserts new rows; we never overwrite."
  }
  column {
    name    = "model_deployment"
    type    = "string"
    comment = "Azure OpenAI deployment name used by the judge (e.g., 'gpt-4o'). Pinned per Judge instance. Required for SOX reproducibility."
  }
  column {
    name    = "evaluated_at"
    type    = "timestamp"
    comment = "UTC timestamp at judge call return time."
  }
}

resource "databricks_sql_table" "gold_layer3_decisions" {
  catalog_name       = databricks_catalog.this.name
  schema_name        = databricks_schema.this["gold"].name
  name               = "layer3_decisions"
  table_type         = "MANAGED"
  data_source_format = "DELTA"

  comment = "Step 7 Layer-3 supervisor write target. One row per multi-agent investigation. Holds the final verdict + reasoning chain + tool trace (JSON-serialised investigation_log). Append-only by investigation_run_id — re-running the supervisor on the same scope produces a new row, not an overwrite. Design choice locked in step_07_task_00 (Option C from Step 6 Design rationale § B): slim verdict + trace pointer over single-table-with-subject-layer or sibling-judge-outcomes. step_07_task_00."

  column {
    name    = "agent_run_id"
    type    = "string"
    comment = "Per-SUPERVISOR-SWEEP run identifier. Shared across all investigations in a single run_layer3 sweep. Joins to gold.cost_telemetry for sweep-level cost rollups."
  }
  column {
    name    = "investigation_run_id"
    type    = "string"
    comment = "Per-INVESTIGATION ULID — unique per supervisor.invoke() call. The natural primary key. Joins to gold.judge_outcomes(_layer3?) once task_07 picks the judge-side storage shape."
  }
  column {
    name    = "engagement_id"
    type    = "string"
    comment = "Engagement identifier — denormalised so Step 7's downstream consumers can filter by tenant scope without joining."
  }
  column {
    name    = "control_id"
    type    = "string"
    comment = "SOX control under investigation (e.g., 'DC-9' for billing rate change; 'DC-2' for variance plausibility). Denormalised."
  }
  column {
    name    = "attribute_id"
    type    = "string"
    comment = "Layer-3-eligible attribute identifier — DC-9.D (billing_rate_change exception) or DC-2.B (variance_plausibility exception) in v1. Denormalised."
  }
  column {
    name    = "quarter"
    type    = "string"
    comment = "Audit period (e.g., 'Q3'). Denormalised."
  }
  column {
    name    = "exception_type"
    type    = "string"
    comment = "Which Layer-3 investigation path fired: 'billing_rate_change' (DC-9.D) or 'variance_plausibility' (DC-2.B). Step 5 Decision 2 expanded scope to two exception types; this column lets downstream filter by path."
  }
  column {
    name    = "final_verdict"
    type    = "string"
    comment = "Supervisor's final verdict: 'pass' (investigation concluded the exception is authorised) | 'fail' (concluded the exception is NOT authorised) | 'uncertain' (escalated_to_human or failed)."
  }
  column {
    name    = "final_confidence"
    type    = "double"
    comment = "Supervisor's final confidence in [0, 1]. The 3-iteration ceiling means this can be set without the supervisor having actually converged — read jointly with status."
  }
  column {
    name    = "iterations_used"
    type    = "int"
    comment = "Number of supervisor → sub-agent → supervisor cycles consumed. Bounded by the 3-iteration ceiling (master plan engineering decision)."
  }
  column {
    name    = "status"
    type    = "string"
    comment = "Terminal state machine state: 'concluded' (verdict trusted) | 'escalated_to_human' (confidence below threshold OR iteration cap hit) | 'failed' (sub-agent exception preserved partial state for postmortem). Every supervisor run lands exactly one of these; never silent failure."
  }
  column {
    name    = "narrative_text"
    type    = "string"
    comment = "The final ExceptionNarrative content (≤200 words). On escalate, this is the degraded handoff text ('Automated investigation could not reach sufficient confidence; human review required.'); on conclude, the grounded exception narrative produced by the narrative sub-agent."
  }
  column {
    name    = "citations"
    type    = "array<string>"
    comment = "Evidence cell refs / IMA-amendment locations the narrative cited. Empty array on escalate. Parallel to gold.narratives.cited_fields."
  }
  column {
    name    = "recommendation"
    type    = "string"
    comment = "ExceptionNarrative.recommendation: 'ACCEPT' (concluded path) or 'ESCALATE' (escalated_to_human path). Downstream consumers (Step 13 review UI) route on this column."
  }
  column {
    name    = "tool_trace"
    type    = "string"
    comment = "JSON-serialised InvestigationState.investigation_log — full reasoning chain across all supervisor → sub-agent cycles. Stored as string (not struct) so the schema doesn't lock into a specific log-entry shape; downstream consumers parse on read. THE auditability artefact — without this, the Layer-3 verdict is unreviewable."
  }
  column {
    name    = "judge_verdict"
    type    = "string"
    comment = "Denormalised from the synchronous judge call at conclude time (Step 6 believe-either-fail gate, wired in task_07). 'pass' / 'fail' / 'uncertain'. NULL on escalate paths where the judge wasn't consulted. Filterable without parsing tool_trace."
  }
  column {
    name    = "judge_confidence"
    type    = "double"
    comment = "Denormalised from the same judge call. NULL when judge_verdict is NULL."
  }
  column {
    name    = "prompt_version"
    type    = "string"
    comment = "Composite prompt version string across sub-agents — e.g., 'extraction_v1.0|validation_v1.0|narrative_v1.1'. Pipe-delimited so the column is searchable + parseable. Required for SOX reproducibility — re-running with bumped prompts produces parallel rows under a new investigation_run_id, not overwrites."
  }
  column {
    name    = "model_deployment"
    type    = "string"
    comment = "Azure OpenAI deployment name pinned across all sub-agents (e.g., 'gpt-4o'). Pinned per supervisor instance. Required for SOX reproducibility."
  }
  column {
    name    = "decided_at"
    type    = "timestamp"
    comment = "UTC timestamp at supervisor exit (conclude or escalate). End of the wall-clock window for this investigation; joins with cost_telemetry to compute per-investigation latency."
  }
}
