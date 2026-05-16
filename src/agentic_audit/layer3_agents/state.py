"""``InvestigationState`` — the LangGraph state carried across one
supervisor → sub-agent investigation cycle.

Four pydantic sub-models own each node-boundary payload:

- ``InvestigationStep`` — one entry in the append-only reasoning trace.
- ``ExtractionFindings`` — Extraction sub-agent's structured output.
- ``ValidationFindings`` — Validation sub-agent's structured output.
- ``ExceptionNarrative`` — Narrative sub-agent's structured output.
  Mirrors ``audit_dev.gold.layer3_decisions.{narrative_text, citations,
  recommendation}`` 1:1.

The top-level ``InvestigationState`` is a ``TypedDict`` (not a pydantic
``BaseModel``). LangGraph 1.x reads channel reducers from class-level
``Annotated`` hints; ``BaseModel.__annotations__`` does not expose them
in the same shape. The ``investigation_log`` field is reduced with
``operator.add`` so two sub-agents that both emit a partial-state
update with one log entry merge to a 2-element list instead of one
overwriting the other.

Sub-models stay pydantic so every node boundary still validates its
payload — a malformed extraction or narrative response surfaces
immediately, never propagates into ``gold.layer3_decisions``.

This module has NO LangGraph dependency. The graph skeleton (and the
``StateGraph`` that consumes these annotations) lands in task_02.

See ``privateDocs/step_07_layer3_multiagent.md`` for the wider design
and ``infra/terraform/modules/databricks_uc/tables_gold.tf`` (the
``gold_layer3_decisions`` resource) for the storage schema this state
ultimately serialises into.
"""

from __future__ import annotations

import operator
from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

# typing_extensions.TypedDict (not stdlib typing.TypedDict): pydantic
# on Python <3.12 rejects typing.TypedDict during JSON-schema
# generation, which fires when LangChain @tool resolves a parameter
# annotated as ``Annotated[InvestigationState, InjectedState]`` (Step
# 8 InjectedState binding). typing_extensions is the supported
# backport; on 3.12+ the two are equivalent.
from typing_extensions import TypedDict

from agentic_audit.models.engagement import ControlId, Quarter
from agentic_audit.models.evidence import AttributeId, ExtractedEvidence
from agentic_audit.models.judge import JudgeVerdict

# ── Type aliases ─────────────────────────────────────────────────────

# Which Layer-3 investigation path fired. v1 covers DC-9.D
# (billing_rate_change) and DC-2.B (variance_plausibility). Expanded
# from the master plan's single-type scope by Step 5 Decision 2.
ExceptionType: TypeAlias = Literal["billing_rate_change", "variance_plausibility"]

# Terminal state-machine state for an investigation. ``investigating``
# is the only non-terminal value; every supervisor exit lands on one
# of the other three so no row is ever silently dropped.
Layer3Status: TypeAlias = Literal[
    "investigating",
    "concluded",
    "escalated_to_human",
    "failed",
]

# Downstream routing instruction the Narrative sub-agent emits. Step 13's
# review UI keys off this column directly — keep the enum minimal.
Recommendation: TypeAlias = Literal["ACCEPT", "ESCALATE"]


# ── Sub-models ───────────────────────────────────────────────────────


class InvestigationStep(BaseModel):
    """One supervisor → sub-agent dispatch entry in the reasoning trace.

    Sub-agents emit a 1-element list containing their step; the
    ``operator.add`` reducer on ``InvestigationState.investigation_log``
    concatenates so the full chain across iterations is preserved
    without any node having to read-modify-write the prior log.

    The full investigation_log is JSON-serialised at supervisor exit
    and persisted as ``gold.layer3_decisions.tool_trace`` — the
    auditability artefact for the Layer-3 verdict.
    """

    model_config = ConfigDict(extra="forbid")

    iteration: int = Field(ge=0)
    actor: str = Field(min_length=1)
    action: str = Field(min_length=1)
    timestamp: datetime


class ExtractionFindings(BaseModel):
    """Extraction sub-agent output (task_04).

    Populated subset differs by ``exception_type``: the billing-rate
    fields apply to DC-9.D, the variance fields to DC-2.B. Both
    subsets are kept on one model (rather than a discriminated union)
    at task_01 because the supervisor's routing is exception-type
    agnostic — task_04 may narrow the contract once tool wiring lands.

    ``evidence_anchors`` is the lineage trail (cell refs, IMA amendment
    locations) the Narrative sub-agent later fact-checks against.
    ``confidence`` is the agent's self-report on extraction quality
    (NOT on the validity of the exception itself — that's
    ``ValidationFindings.confidence``).
    """

    model_config = ConfigDict(extra="forbid")

    # Billing-rate-change subset (DC-9.D)
    old_rate: float | None = None
    new_rate: float | None = None
    ima_amendment_found: bool | None = None
    ima_amendment_text: str | None = None

    # Variance-plausibility subset (DC-2.B)
    variance_magnitude: float | None = None
    variance_explanation_found: bool | None = None
    variance_explanation_text: str | None = None

    # Common
    evidence_anchors: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class ValidationFindings(BaseModel):
    """Validation sub-agent output (task_05).

    Single LLM judgment over whether the supporting document
    authorises the exception. When extraction reports "no amendment /
    no explanation found", task_05 short-circuits this stage to
    ``is_authorized=False, confidence=0.9`` without an LLM call — the
    cheap-and-confident negative path.

    ``reasoning`` must cite the document text the judgment rests on;
    enforced ``min_length=1`` here, semantic-grounding is task_05's
    prompt responsibility.
    """

    model_config = ConfigDict(extra="forbid")

    is_authorized: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)


class ExceptionNarrative(BaseModel):
    """Narrative sub-agent output (task_06).

    Mirrors ``gold.layer3_decisions.{narrative_text, citations,
    recommendation}`` 1:1. ``word_count`` capped at 200 (vs Layer 2's
    150) — exceptions need more detail to discharge the recommendation.

    The degraded-escalation path (task_07) constructs a sentinel
    ExceptionNarrative with the canned handoff text,
    ``recommendation="ESCALATE"``, and empty citations — every
    terminal state writes a complete row, never silent failure.
    """

    model_config = ConfigDict(extra="forbid")

    narrative_text: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)
    recommendation: Recommendation
    word_count: int = Field(ge=0, le=200)


# ── Top-level state ──────────────────────────────────────────────────


class InvestigationState(TypedDict, total=False):
    """LangGraph state for one supervisor.invoke() call.

    ``total=False`` so partial-state updates from individual nodes are
    valid TypedDict instances — LangGraph merges them into the running
    state via per-channel reducers (default: overwrite; ``investigation_log``:
    concatenate via ``operator.add``).

    Field groups:

    - **Identity** — ``investigation_run_id`` (per-invocation ULID),
      ``agent_run_id`` (per-supervisor-sweep ID for cost telemetry
      joins).
    - **Scope** — ``engagement_id`` / ``control_id`` / ``attribute_id``
      / ``quarter`` / ``exception_type``. All denormalised onto
      ``gold.layer3_decisions``.
    - **Inputs** — ``current_quarter_evidence`` and
      ``prior_quarter_evidence``. The supervisor never mutates these.
    - **Trace** — ``investigation_log``: append-only via
      ``operator.add``. Serialised to ``gold.layer3_decisions.tool_trace``
      at exit.
    - **Sub-agent outputs** — ``extraction_findings`` /
      ``validation_findings`` / ``final_narrative``. None on entry;
      the supervisor's routing keys off whichever is still None.
    - **Convergence** — ``confidence_score`` (joint signal the
      supervisor's conclude-vs-escalate gate consults),
      ``iterations_used`` (bounded at 3 — see task_07),
      ``status`` (terminal state-machine state).

    See ``privateDocs/step_07_layer3_multiagent.md`` task_01 for the
    full field-by-field rationale.
    """

    investigation_run_id: str
    agent_run_id: str
    engagement_id: str
    control_id: ControlId
    attribute_id: AttributeId
    quarter: Quarter
    exception_type: ExceptionType

    current_quarter_evidence: ExtractedEvidence
    prior_quarter_evidence: ExtractedEvidence

    # Append-only via the operator.add reducer — LangGraph 1.x reads
    # this annotation at compile time. Do NOT change the reducer to
    # anything that overwrites; the trace must accumulate, not flap.
    investigation_log: Annotated[list[InvestigationStep], operator.add]

    extraction_findings: ExtractionFindings | None
    validation_findings: ValidationFindings | None
    final_narrative: ExceptionNarrative | None

    # Populated by supervisor_node when ``final_narrative`` first lands
    # (task_03 wires the gate). Both None until then. Read by
    # ``route_from_supervisor`` for the conclude-vs-escalate decision
    # and serialised to ``gold.layer3_decisions.{judge_verdict,
    # judge_confidence}`` at supervisor exit. NULL on escalate paths
    # where no narrative was produced (e.g. iteration-cap escalate).
    judge_verdict: JudgeVerdict | None
    judge_confidence: float | None

    confidence_score: float
    iterations_used: int
    status: Layer3Status


__all__ = [
    "ExceptionNarrative",
    "ExceptionType",
    "ExtractionFindings",
    "InvestigationState",
    "InvestigationStep",
    "Layer3Status",
    "Recommendation",
    "ValidationFindings",
]
