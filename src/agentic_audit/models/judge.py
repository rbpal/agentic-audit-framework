"""Pydantic model for the LLM-as-judge eval harness (Step 6).

``JudgeResponse`` is the structured output the judge returns after
reading a narrative + evidence + ToC attribute definition + gold
expected verdict. The model rejects malformed output at parse time so
a misbehaving judge surfaces immediately instead of silently
corrupting ``audit_dev.gold.eval_outcomes``.

The judge decision contract (encoded in
``layer2_narrative/prompts/judge_v1_0.txt``):

- ``verdict`` is one of ``pass``, ``fail``, ``uncertain`` — no other
  values accepted.
- ``confidence`` is a float in ``[0.0, 1.0]`` — calibration sanity.
- ``reasoning`` is non-empty — a bare verdict with no rationale is a
  prompt failure, not an acceptable output.
- ``cited_evidence_fields`` lists the evidence JSON keys the judge
  inspected. Empty list is allowed for ``uncertain`` (the judge
  legitimately couldn't find supporting evidence) but rejected for
  ``pass``/``fail`` (Decision Rule 1 in the prompt).

See ``privateDocs/step_06_eval_harness.md`` task_02 for the full
design rationale.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from agentic_audit.models.engagement import ControlId, Quarter
from agentic_audit.models.evidence import AttributeId

JudgeVerdict = Literal["pass", "fail", "uncertain"]
JudgeStatus = Literal["ok", "parse_failure", "validation_failure", "empty_content"]
FactCheckVerdict = Literal["pass", "fail"]


class JudgeResponse(BaseModel):
    """Structured output from one LLM-as-judge call.

    Mirrors ``audit_dev.gold.eval_outcomes`` columns 1:1 (judge_verdict,
    judge_confidence, judge_reasoning, cited_evidence_fields). Other
    eval-outcome columns (agent_run_id, narrative_run_id,
    fact_check_verdict, prompt_version, model_deployment, evaluated_at)
    are populated by the sweep driver around this payload.
    """

    verdict: JudgeVerdict
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)
    cited_evidence_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _pass_or_fail_must_cite_evidence(self) -> JudgeResponse:
        """Decision Rule 1 in the judge prompt: pass/fail verdicts MUST
        cite specific evidence fields. A judge that issues a verdict
        with no cited fields is either lazy or hallucinating; reject at
        the boundary. ``uncertain`` is exempt — citing nothing is the
        legitimate "evidence is silent" case."""
        if self.verdict in ("pass", "fail") and not self.cited_evidence_fields:
            raise ValueError(
                f"verdict={self.verdict!r} requires at least one entry in "
                "cited_evidence_fields (Decision Rule 1 in judge_v1_0.txt). "
                "Use verdict='uncertain' if no evidence supports a verdict."
            )
        return self


class JudgeOutcomeRow(BaseModel):
    """One row destined for ``audit_dev.gold.judge_outcomes``.

    Composes:

    - the judge's structured verdict (``judge_*`` fields, mirrored from
      ``JudgeResponse``),
    - denormalised scope from the narrative being judged
      (``engagement_id`` / ``control_id`` / ``attribute_id`` /
      ``quarter`` / ``narrative_run_id`` / ``fact_check_verdict``),
    - per-sweep state (``judge_run_id``, ``evaluated_at``,
      ``judge_status``),
    - the ground-truth verdict from the engagement ToC
      (``gold_expected_verdict``),
    - reproducibility pins (``prompt_version``, ``model_deployment``)
      — the judge's prompt + model, NOT the narrative's. The column
      documents which judge configuration produced THIS verdict.

    The 16 fields match the Terraform schema in
    ``infra/terraform/modules/databricks_uc/tables_gold.tf`` 1:1.
    See ``privateDocs/step_06_eval_harness.md`` task_04 for the full
    schema rationale.
    """

    judge_run_id: str = Field(min_length=1)
    narrative_run_id: str = Field(min_length=1)
    engagement_id: str = Field(min_length=1)
    control_id: ControlId
    attribute_id: AttributeId
    quarter: Quarter
    judge_verdict: JudgeVerdict
    judge_confidence: float = Field(ge=0.0, le=1.0)
    judge_reasoning: str = Field(min_length=1)
    cited_evidence_fields: list[str] = Field(default_factory=list)
    judge_status: JudgeStatus
    gold_expected_verdict: str = Field(min_length=1)
    fact_check_verdict: FactCheckVerdict
    prompt_version: str = Field(min_length=1)
    model_deployment: str = Field(min_length=1)
    evaluated_at: datetime


__all__ = [
    "FactCheckVerdict",
    "JudgeOutcomeRow",
    "JudgeResponse",
    "JudgeStatus",
    "JudgeVerdict",
]
