"""Layer-3 decision row model — exact shape of one row in
``audit_dev.gold.layer3_decisions``.

Produced by ``run_investigation`` at supervisor exit (concluded /
escalated_to_human / failed) and persisted by
``Layer3DecisionsWriter``. Grain: one row per ``investigation_run_id``
(per-investigation ULID). Append-only — re-running the supervisor on
the same scope produces a new row, NOT an overwrite, so the audit
trail preserves prompt-version + model-deployment cohorts.

The model mirrors the Terraform-managed table 1:1 (column names,
types, nullability). Schema source of truth:
``infra/terraform/modules/databricks_uc/tables_gold.tf`` →
``databricks_sql_table.gold_layer3_decisions``.

See ``privateDocs/step_07_layer3_multiagent.md`` step_07_task_00
for the Option-C design rationale (slim verdict + JSON trace pointer
over single-table-with-subject-layer or sibling-judge-outcomes).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

# ── Type aliases (subset of state.py's terminal enums + table schema) ─

# Final verdict the supervisor commits to gold. The table comment locks
# this enum: 'pass' / 'fail' / 'uncertain'. The supervisor maps from
# its own (status, recommendation) joint signal to this 3-value verdict
# at row-build time — the table schema is intentionally simpler than
# the in-process state.
FinalVerdict: TypeAlias = Literal["pass", "fail", "uncertain"]

# Mirrors state.Layer3Status's terminal subset. ``investigating`` is
# in-process only; never persisted.
PersistedStatus: TypeAlias = Literal["concluded", "escalated_to_human", "failed"]

# Mirrors state.Recommendation. Persisted verbatim.
PersistedRecommendation: TypeAlias = Literal["ACCEPT", "ESCALATE"]


class Layer3Decision(BaseModel):
    """One row of ``audit_dev.gold.layer3_decisions``.

    Built once per ``run_investigation`` exit by
    ``build_layer3_decision_row``. Persisted by
    ``Layer3DecisionsWriter.write_decision``.

    Fields are 1:1 with the Terraform schema. Validators pin the
    invariants the table itself can't enforce (verdict ↔ status
    coherence, decided_at ≥ a sensible epoch, etc.) so a malformed
    row surfaces at construction, not at the warehouse.
    """

    model_config = ConfigDict(extra="forbid")

    # Identity ----------------------------------------------------------
    agent_run_id: str = Field(min_length=1)
    investigation_run_id: str = Field(min_length=1)

    # Scope -------------------------------------------------------------
    engagement_id: str = Field(min_length=1)
    control_id: str = Field(min_length=1)
    attribute_id: str = Field(min_length=1)
    quarter: str = Field(min_length=1)
    exception_type: str = Field(min_length=1)

    # Verdict -----------------------------------------------------------
    final_verdict: FinalVerdict
    final_confidence: float = Field(ge=0.0, le=1.0)
    iterations_used: int = Field(ge=0)
    status: PersistedStatus

    # Narrative ---------------------------------------------------------
    narrative_text: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)
    recommendation: PersistedRecommendation

    # Auditability ------------------------------------------------------
    # JSON-serialised investigation_log. Stored as string per the table
    # schema's ``tool_trace`` column comment: "Stored as string (not
    # struct) so the schema doesn't lock into a specific log-entry shape;
    # downstream consumers parse on read."
    tool_trace: str = Field(min_length=2)  # "[]" is the minimal valid JSON

    # Judge gate (Step 6 believe-either-fail wiring) --------------------
    # NULL on escalate paths where the judge wasn't consulted.
    judge_verdict: str | None = None
    judge_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    # Reproducibility ---------------------------------------------------
    # Composite pipe-delimited per the table comment, e.g.
    # "extraction_v1.0|validation_v1.0|narrative_v1.1".
    prompt_version: str = Field(min_length=1)
    model_deployment: str = Field(min_length=1)

    decided_at: datetime


__all__ = [
    "FinalVerdict",
    "Layer3Decision",
    "PersistedRecommendation",
    "PersistedStatus",
]
