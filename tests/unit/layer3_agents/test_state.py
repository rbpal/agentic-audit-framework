"""Tests for ``agentic_audit.layer3_agents.state``.

Two contracts pinned here:

1. Each sub-model rejects malformed input at construction (shape
   pinning — the same boundary discipline as
   ``tests/unit/models/test_narrative_models.py``).
2. The ``operator.add`` reducer on ``investigation_log`` is wired
   correctly. Two assertions: (a) ``operator.add`` is present in the
   ``Annotated`` metadata so LangGraph 1.x will discover it at
   compile time, and (b) it actually concatenates disjoint partial
   updates — the merge LangGraph performs when two nodes return
   overlapping partial state in the same superstep.

The reducer test runs without any LangGraph import — task_01 is
schema-only.
"""

from __future__ import annotations

import operator
from datetime import UTC, datetime
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from agentic_audit.layer3_agents.state import (
    ExceptionNarrative,
    ExtractionFindings,
    InvestigationState,
    InvestigationStep,
    ValidationFindings,
)
from agentic_audit.models.evidence import (
    ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)

UTC_TS = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


def _make_evidence(control_id: str, quarter: str) -> ExtractedEvidence:
    """Minimal valid ExtractedEvidence honouring per-control attr counts.

    DC-2 → A-D (4 attrs); DC-9 → A-F (6 attrs). The cross-field
    validator on ExtractedEvidence rejects any deviation, so this
    helper keeps the test bodies focused on the layer-3 schema.
    """
    return ExtractedEvidence(
        engagement_id="eng-1",
        control_id=control_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        run_id="run-1",
        extraction_timestamp=UTC_TS,
        preparer=SignOff(initials="JD", role="preparer", date=UTC_TS),
        reviewer=SignOff(initials="MR", role="reviewer", date=UTC_TS),
        attributes=[
            AttributeCheck(
                control_id=control_id,  # type: ignore[arg-type]
                attribute_id=a,  # type: ignore[arg-type]
                status="pass",
            )
            for a in ATTRIBUTES_PER_CONTROL[control_id]
        ],
        source_bronze_file_hash="abc",
    )


# ── InvestigationStep ────────────────────────────────────────────────


def test_investigation_step_happy_path() -> None:
    step = InvestigationStep(
        iteration=0,
        actor="supervisor",
        action="route_to_extraction",
        timestamp=UTC_TS,
    )
    assert step.actor == "supervisor"
    assert step.iteration == 0


def test_investigation_step_rejects_empty_actor() -> None:
    with pytest.raises(ValidationError):
        InvestigationStep(iteration=0, actor="", action="x", timestamp=UTC_TS)


def test_investigation_step_rejects_negative_iteration() -> None:
    with pytest.raises(ValidationError):
        InvestigationStep(iteration=-1, actor="supervisor", action="x", timestamp=UTC_TS)


# ── ExtractionFindings ───────────────────────────────────────────────


def test_extraction_findings_billing_rate_subset() -> None:
    f = ExtractionFindings(
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_found=True,
        ima_amendment_text="Board-approved amendment effective Q3.",
        evidence_anchors=["DC-9 Billing!r3c4", "IMA-amendment-2026-03-15"],
        confidence=0.9,
    )
    assert f.ima_amendment_found is True
    # Cross-subset stays clean — variance fields untouched.
    assert f.variance_magnitude is None
    assert f.variance_explanation_found is None


def test_extraction_findings_variance_subset() -> None:
    f = ExtractionFindings(
        variance_magnitude=0.18,
        variance_explanation_found=True,
        variance_explanation_text="Pension contribution timing shift Q2→Q3.",
        evidence_anchors=["DC-2 Variance!r5c2"],
        confidence=0.8,
    )
    assert f.variance_explanation_found is True
    assert f.old_rate is None


def test_extraction_findings_rejects_confidence_above_one() -> None:
    with pytest.raises(ValidationError):
        ExtractionFindings(confidence=1.1)


def test_extraction_findings_rejects_confidence_below_zero() -> None:
    with pytest.raises(ValidationError):
        ExtractionFindings(confidence=-0.1)


# ── ValidationFindings ───────────────────────────────────────────────


def test_validation_findings_happy_path() -> None:
    f = ValidationFindings(
        is_authorized=True,
        confidence=0.85,
        reasoning="Amendment text explicitly covers the rate change.",
    )
    assert f.is_authorized is True


def test_validation_findings_no_document_fast_path() -> None:
    """task_05's cheap-and-confident negative path — high confidence
    in the negative when extraction reports no supporting document."""
    f = ValidationFindings(
        is_authorized=False,
        confidence=0.9,
        reasoning="No supporting document found",
    )
    assert f.is_authorized is False
    assert f.confidence == 0.9


def test_validation_findings_rejects_empty_reasoning() -> None:
    with pytest.raises(ValidationError):
        ValidationFindings(is_authorized=True, confidence=0.9, reasoning="")


# ── ExceptionNarrative ───────────────────────────────────────────────


def test_exception_narrative_accept_path() -> None:
    n = ExceptionNarrative(
        narrative_text="Rate change 28.5 → 30.0 authorised by IMA amendment dated 2026-03-15.",
        citations=["DC-9 Billing!r3c4", "IMA-amendment-2026-03-15"],
        recommendation="ACCEPT",
        word_count=12,
    )
    assert n.recommendation == "ACCEPT"
    assert len(n.citations) == 2


def test_exception_narrative_escalate_path() -> None:
    """The degraded-escalation sentinel narrative — empty citations
    with the canned handoff text. task_07 constructs this on every
    escalate path so no row lacks a narrative."""
    n = ExceptionNarrative(
        narrative_text="Automated investigation could not reach sufficient confidence; human review required.",
        citations=[],
        recommendation="ESCALATE",
        word_count=11,
    )
    assert n.recommendation == "ESCALATE"
    assert n.citations == []


def test_exception_narrative_rejects_word_count_over_200() -> None:
    with pytest.raises(ValidationError):
        ExceptionNarrative(
            narrative_text="x",
            recommendation="ACCEPT",
            word_count=201,
        )


def test_exception_narrative_rejects_invalid_recommendation() -> None:
    with pytest.raises(ValidationError):
        ExceptionNarrative(
            narrative_text="x",
            recommendation="MAYBE",  # type: ignore[arg-type]
            word_count=1,
        )


def test_exception_narrative_rejects_empty_text() -> None:
    with pytest.raises(ValidationError):
        ExceptionNarrative(
            narrative_text="",
            recommendation="ACCEPT",
            word_count=0,
        )


# ── InvestigationState ───────────────────────────────────────────────


def _make_state(
    control_id: str,
    attribute_id: str,
    exception_type: str,
) -> InvestigationState:
    return {
        "investigation_run_id": "01HXYZINVESTIGATION0001",
        "agent_run_id": "sweep-001",
        "engagement_id": "eng-1",
        "control_id": control_id,  # type: ignore[typeddict-item]
        "attribute_id": attribute_id,  # type: ignore[typeddict-item]
        "quarter": "Q3",
        "exception_type": exception_type,  # type: ignore[typeddict-item]
        "current_quarter_evidence": _make_evidence(control_id, "Q3"),
        "prior_quarter_evidence": _make_evidence(control_id, "Q2"),
        "investigation_log": [],
        "extraction_findings": None,
        "validation_findings": None,
        "final_narrative": None,
        "confidence_score": 0.0,
        "iterations_used": 0,
        "status": "investigating",
    }


def test_investigation_state_constructs_billing_rate_change() -> None:
    state = _make_state("DC-9", "D", "billing_rate_change")
    assert state["exception_type"] == "billing_rate_change"
    assert state["control_id"] == "DC-9"
    assert state["attribute_id"] == "D"
    assert state["status"] == "investigating"


def test_investigation_state_constructs_variance_plausibility() -> None:
    state = _make_state("DC-2", "B", "variance_plausibility")
    assert state["exception_type"] == "variance_plausibility"
    assert state["control_id"] == "DC-2"
    assert state["attribute_id"] == "B"


# ── investigation_log reducer ────────────────────────────────────────


def test_investigation_log_reducer_is_operator_add() -> None:
    """Pin the reducer here. A future refactor that drops the
    ``Annotated`` wrapper would silently change LangGraph's merge
    semantics from concatenate to overwrite — losing the trace
    across iterations. This test catches that drift at unit-test
    time, before any integration test even runs.
    """
    hints = get_type_hints(InvestigationState, include_extras=True)
    log_hint = hints["investigation_log"]
    assert operator.add in log_hint.__metadata__, (
        "investigation_log must be reduced with operator.add — "
        "any other reducer breaks append-only trace semantics."
    )


def test_investigation_log_reducer_merges_disjoint_partial_updates() -> None:
    """The exact merge LangGraph performs when two nodes return
    overlapping partial state in the same superstep — each node emits
    a 1-element list, the reducer concatenates."""
    step_a = InvestigationStep(
        iteration=0,
        actor="supervisor",
        action="route_to_extraction",
        timestamp=UTC_TS,
    )
    step_b = InvestigationStep(
        iteration=1,
        actor="extraction_agent",
        action="found_ima_amendment",
        timestamp=UTC_TS,
    )
    merged = operator.add([step_a], [step_b])
    assert merged == [step_a, step_b]
    assert len(merged) == 2
