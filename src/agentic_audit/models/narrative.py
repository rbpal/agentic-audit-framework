"""Pydantic models for Layer 2 grounded narrative generation.

Four models, each owning one boundary:

- ``NarrativeRequest`` — what the prompt template renders against
  (the inputs the generator hands to ``str.Template`` substitution).
- ``NarrativeResponse`` — the JSON-mode shape Azure OpenAI must return.
  Validated immediately on parse; a malformed response is rejected at
  the boundary, never propagates.
- ``FactCheckResult`` — verdict from ``FactChecker`` on a narrative.
  Inlined into ``AttributeNarrative`` before gold-table write.
- ``AttributeNarrative`` — what the gold writer persists. Wraps the
  ``NarrativeResponse`` with generation metadata (prompt version, model
  deployment, run id, timestamp) and inlined fact-check outcome.
  Mirrors the ``audit_dev.gold.narratives`` Delta table column-for-column.

Every field is validated at the layer boundary so by the time a record
reaches gold it's already structurally correct.

See ``privateDocs/step_05_layer2_narrative.md`` (Decisions 2, 3, 5) for
why the schema looks the way it does.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from agentic_audit.models.engagement import ControlId, Quarter
from agentic_audit.models.evidence import AttributeId


class NarrativeRequest(BaseModel):
    """Inputs to the prompt template at render time.

    ``evidence_json`` is the serialized ``ExtractedEvidence`` payload
    (already JSON-encoded so the template can drop it in verbatim).
    Validation here is shape-only — the generator owns rendering.
    """

    control_id: ControlId
    attribute_id: AttributeId
    quarter: Quarter
    evidence_json: str = Field(min_length=1)


class NarrativeResponse(BaseModel):
    """Structured shape Azure OpenAI must return under JSON mode.

    The prompt instructs GPT-4o to emit exactly this schema. Pydantic
    rejects any deviation at parse time. A malformed response is a
    generator-level failure, not a silent narrative drift.
    """

    narrative_text: str = Field(min_length=1)
    cited_fields: list[str] = Field(default_factory=list)
    word_count: int = Field(ge=0)


class FactCheckResult(BaseModel):
    """Verdict of ``FactChecker.check()`` on a narrative.

    The invariant ``passed == (len(issues) == 0)`` is enforced — a caller
    that constructs ``passed=True`` with non-empty issues, or
    ``passed=False`` with no issues, gets a ``ValidationError``. This
    forecloses a class of caller bugs where the verdict and the evidence
    list drift out of sync.
    """

    passed: bool
    issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _passed_iff_no_issues(self) -> FactCheckResult:
        if self.passed and self.issues:
            raise ValueError("passed=True is incompatible with non-empty issues")
        if not self.passed and not self.issues:
            raise ValueError("passed=False requires at least one issue")
        return self


class AttributeNarrative(BaseModel):
    """Gold-writer payload — one row in ``audit_dev.gold.narratives``.

    Wraps ``NarrativeResponse`` content with the lineage + reproducibility
    metadata the gold table requires. ``prompt_version`` is the git-pinned
    template version (e.g. ``"v1.0"``); ``model_deployment`` is the Azure
    deployment name (``"gpt-4o"``); ``generation_run_id`` ties this
    narrative back to a single sweep invocation in
    ``audit_dev.gold.cost_telemetry``; ``narrative_call_id`` is the
    per-call ULID that uniquely identifies one ``generate()`` invocation
    (Step 5 follow-up #5).

    ``fact_check_passed`` defaults False so an unchecked narrative is
    safely-conservative; the fact-checker (task_05) flips it after
    verification.
    """

    engagement_id: str = Field(min_length=1)
    control_id: ControlId
    attribute_id: AttributeId
    quarter: Quarter
    source_evidence_id: str = Field(min_length=1)

    narrative_text: str = Field(min_length=1)
    cited_fields: list[str] = Field(default_factory=list)
    word_count: int = Field(ge=0)

    prompt_version: str = Field(min_length=1)
    model_deployment: str = Field(min_length=1)
    generation_run_id: str = Field(min_length=1)
    # Per-call ULID — optional so historical v1.0 rows (written before
    # the column existed) round-trip cleanly through the reader. New
    # rows from the generator always populate it.
    narrative_call_id: str | None = None
    generated_at: datetime

    fact_check_passed: bool = False
    fact_check_issues: list[str] = Field(default_factory=list)


__all__ = [
    "AttributeNarrative",
    "FactCheckResult",
    "NarrativeRequest",
    "NarrativeResponse",
]
