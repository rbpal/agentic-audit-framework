"""Unit tests for ``agentic_audit.layer3_agents.tools`` — first
dedicated tests for the Layer-3 ReAct tools (Step 8 task_01).

This file covers the **real-body** tools that read from the
supervisor-loaded ``InvestigationState`` via ``InjectedState``. The
placeholder tools (``compare_billing_rates``, ``read_reviewer_comments``)
keep their shape-only tests in ``test_extraction_agent.py`` until
task_02 + task_03 swap their bodies.

Tools are invoked with the LLM-facing args via ``tool.invoke(...)`` —
the ``state`` parameter is injected by LangGraph at runtime, but in
unit tests we pass it directly as the same dict the LLM would never
see. Keeps the test seam fast (no graph compile, no LLM call) while
still exercising the real projection logic.

Step 8 task_00's PoC verified the framework-side injection wiring
end-to-end against the live tenant (see
``privateDocs/step_08_agent_tools.md`` § task_00 Outcome); these
tests verify the projection LOGIC the framework hands state to.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agentic_audit.layer3_agents.tools import (
    _coerce_rate,
    _find_attribute_check,
    _resolve_evidence_for_quarter,
    read_billing_rate,
)
from agentic_audit.models.evidence import (
    ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)

UTC_TS = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)


# ── Fixtures ─────────────────────────────────────────────────────────


def _evidence(
    *,
    control_id: str = "DC-9",
    quarter: str = "Q3",
    dc9d_rate: Any = 30.0,
    dc9d_cell_refs: list[str] | None = None,
    dc9d_notes: str | None = None,
    omit_dc9d: bool = False,
) -> ExtractedEvidence:
    """Build a synthetic ExtractedEvidence with a configurable DC-9.D
    attribute check. Other attributes get default pass-status entries
    so the ExtractedEvidence's per-attribute count validator is
    satisfied."""
    attrs: list[AttributeCheck] = []
    for attr_id in ATTRIBUTES_PER_CONTROL[control_id]:
        if attr_id == "D" and omit_dc9d:
            continue
        if attr_id == "D":
            attrs.append(
                AttributeCheck(
                    control_id=control_id,  # type: ignore[arg-type]
                    attribute_id="D",
                    status="pass",
                    evidence_cell_refs=dc9d_cell_refs or [],
                    extracted_value=dc9d_rate,
                    notes=dc9d_notes,
                )
            )
        else:
            attrs.append(
                AttributeCheck(
                    control_id=control_id,  # type: ignore[arg-type]
                    attribute_id=attr_id,  # type: ignore[arg-type]
                    status="pass",
                )
            )
    # If omit_dc9d emptied the D slot, the remaining attrs may dip
    # below min_length=4; we accept that and let pydantic enforce the
    # contract on the call site.
    return ExtractedEvidence(
        engagement_id="eng-1",
        control_id=control_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        run_id="run-1",
        extraction_timestamp=UTC_TS,
        preparer=SignOff(initials="JD", role="preparer", date=UTC_TS),
        reviewer=SignOff(initials="MR", role="reviewer", date=UTC_TS),
        attributes=attrs,
        source_bronze_file_hash="abc",
    )


def _state(
    *,
    current: ExtractedEvidence,
    prior: ExtractedEvidence,
) -> dict[str, Any]:
    """Build a _ExtractionReActState-shaped dict for tool.invoke tests.

    Includes both the InvestigationState fields (the tool's body
    reads these via state.get(...)) AND the AgentState fields
    (``messages`` required, ``remaining_steps`` optional) — LangChain
    pydantic-validates the full schema when binding ``state`` through
    ``tool.invoke({"state": ...})``, so we have to populate both.
    """
    return {
        # AgentState side
        "messages": [],
        # InvestigationState side
        "investigation_run_id": "inv-test",
        "agent_run_id": "sweep-1",
        "engagement_id": "eng-1",
        "control_id": "DC-9",
        "attribute_id": "D",
        "quarter": current.quarter,
        "exception_type": "billing_rate_change",
        "current_quarter_evidence": current,
        "prior_quarter_evidence": prior,
        "investigation_log": [],
        "extraction_findings": None,
        "validation_findings": None,
        "final_narrative": None,
        "judge_verdict": None,
        "judge_confidence": None,
        "confidence_score": 0.0,
        "iterations_used": 0,
        "status": "investigating",
    }


# ── _resolve_evidence_for_quarter ────────────────────────────────────


def test_resolve_picks_current_quarter() -> None:
    current = _evidence(quarter="Q3")
    prior = _evidence(quarter="Q2")
    state = _state(current=current, prior=prior)
    assert _resolve_evidence_for_quarter(state, "Q3") is current


def test_resolve_picks_prior_quarter() -> None:
    current = _evidence(quarter="Q3")
    prior = _evidence(quarter="Q2")
    state = _state(current=current, prior=prior)
    assert _resolve_evidence_for_quarter(state, "Q2") is prior


def test_resolve_returns_none_for_unknown_quarter() -> None:
    """LLM asks for a quarter the supervisor didn't load → None.
    Surfaces as rate=None in the tool's return; agent loop handles."""
    current = _evidence(quarter="Q3")
    prior = _evidence(quarter="Q2")
    state = _state(current=current, prior=prior)
    assert _resolve_evidence_for_quarter(state, "Q1") is None


# ── _find_attribute_check ────────────────────────────────────────────


def test_find_attribute_check_hits_dc9d() -> None:
    evidence = _evidence(dc9d_rate=28.5)
    check = _find_attribute_check(evidence, "D")
    assert check is not None
    assert check.attribute_id == "D"
    assert check.extracted_value == 28.5


def test_find_attribute_check_returns_none_when_missing() -> None:
    """Asking for an attribute the control doesn't carry returns
    None — guard against a future control whose rate lives on a
    different attribute, or a partial evidence row."""
    evidence = _evidence()
    assert _find_attribute_check(evidence, "Z") is None


# ── _coerce_rate ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (30.0, 30.0),
        (28, 28.0),
        ("30.0", 30.0),
        ("28.5", 28.5),
        (None, None),
        ("not-a-number", None),
        ([1, 2], None),
        ({}, None),
    ],
)
def test_coerce_rate_handles_typed_and_stringified_numerics(
    value: Any, expected: float | None
) -> None:
    """Layer-1 occasionally stores rates as strings depending on the
    source cell type; the tool tolerates both. Non-numeric or
    non-string values degrade to None rather than raising — keeps
    the ReAct loop progressing on dirty evidence."""
    assert _coerce_rate(value) == expected


# ── read_billing_rate — happy paths ──────────────────────────────────


def test_read_billing_rate_current_quarter_happy_path() -> None:
    """Standard DC-9.D Q3 lookup: numeric extracted_value, populated
    cell refs, real timestamp. All five projection fields are
    present + non-empty."""
    current = _evidence(
        quarter="Q3",
        dc9d_rate=30.0,
        dc9d_cell_refs=["sheet1!A12", "amendment.pdf!p2"],
        dc9d_notes="Q3 rate per IMA amendment.",
    )
    prior = _evidence(quarter="Q2", dc9d_rate=28.5)
    state = _state(current=current, prior=prior)

    result = read_billing_rate.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "quarter": "Q3",
            "state": state,
        }
    )

    assert result["rate"] == 30.0
    assert result["rate_unit"] == "basis_points"
    assert result["source_cell_ref"] == "sheet1!A12"
    assert result["recorded_at"] == UTC_TS.isoformat()
    assert result["notes"] == "Q3 rate per IMA amendment."


def test_read_billing_rate_prior_quarter_happy_path() -> None:
    """Same engagement, different quarter — must resolve to prior."""
    current = _evidence(quarter="Q3", dc9d_rate=30.0)
    prior = _evidence(
        quarter="Q2",
        dc9d_rate=28.5,
        dc9d_cell_refs=["sheet1!A11"],
    )
    state = _state(current=current, prior=prior)

    result = read_billing_rate.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "quarter": "Q2",
            "state": state,
        }
    )

    assert result["rate"] == 28.5
    assert result["source_cell_ref"] == "sheet1!A11"


def test_read_billing_rate_uses_first_cell_ref_when_multiple_present() -> None:
    """Lineage anchor convention: first cell ref is the canonical
    one. Multi-ref attribute checks (e.g. amendment + reconciliation
    refs) project the first as source; downstream consumers can
    cross-walk via the full list on the evidence row."""
    current = _evidence(
        quarter="Q3",
        dc9d_cell_refs=["sheet1!A12", "sheet1!B14", "amendment.pdf!p2"],
    )
    prior = _evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    result = read_billing_rate.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "quarter": "Q3",
            "state": state,
        }
    )
    assert result["source_cell_ref"] == "sheet1!A12"


# ── read_billing_rate — degraded paths ───────────────────────────────


def test_read_billing_rate_unknown_quarter_degrades_to_null() -> None:
    """Quarter not in state → rate=None + diagnostic note. NOT an
    exception; the agent loop is expected to continue and the
    narrative will surface 'unknown rate'."""
    current = _evidence(quarter="Q3")
    prior = _evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    result = read_billing_rate.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "quarter": "Q1",  # neither current nor prior
            "state": state,
        }
    )
    assert result["rate"] is None
    assert result["source_cell_ref"] == ""
    assert result["recorded_at"] == ""
    assert "Q1" in result["notes"]
    assert "not in" in result["notes"]


def test_read_billing_rate_non_numeric_extracted_value_degrades_to_null() -> None:
    """Layer-1 cell extraction stored something non-numeric in the
    rate slot (e.g. a malformed sheet cell with text). The tool
    must NOT raise — degrades to rate=None and lets the agent
    continue."""
    current = _evidence(quarter="Q3", dc9d_rate="see notes")
    prior = _evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    result = read_billing_rate.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "quarter": "Q3",
            "state": state,
        }
    )
    assert result["rate"] is None
    # Other fields still populated — only the numeric coercion failed
    assert result["recorded_at"] == UTC_TS.isoformat()


def test_read_billing_rate_attribute_check_with_no_cell_refs() -> None:
    """A DC-9.D check with no evidence_cell_refs → empty string for
    source_cell_ref. Doesn't break the projection; downstream
    consumers see the empty string as 'no lineage anchor'."""
    current = _evidence(quarter="Q3", dc9d_cell_refs=[])
    prior = _evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    result = read_billing_rate.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "quarter": "Q3",
            "state": state,
        }
    )
    assert result["source_cell_ref"] == ""
    assert result["rate"] == 30.0  # other projections still work


def test_read_billing_rate_attribute_check_with_no_notes() -> None:
    """notes=None on the AttributeCheck → empty string in projection
    (not the literal string 'None'). Same shape as the placeholder
    was returning so the agent prompt doesn't need to special-case."""
    current = _evidence(quarter="Q3", dc9d_notes=None)
    prior = _evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    result = read_billing_rate.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "quarter": "Q3",
            "state": state,
        }
    )
    assert result["notes"] == ""


# ── Shape contract (preserved from placeholder) ──────────────────────


def test_read_billing_rate_always_returns_five_keys() -> None:
    """The return-dict shape is part of the agent prompt's contract.
    Whether the tool hit a happy path or degraded to nulls, the
    same five keys must be present so the prompt's few-shot
    examples don't break."""
    current = _evidence(quarter="Q3")
    prior = _evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    expected_keys = {"rate", "rate_unit", "source_cell_ref", "recorded_at", "notes"}

    # Happy path
    happy = read_billing_rate.invoke(
        {"engagement_id": "eng-1", "control_id": "DC-9", "quarter": "Q3", "state": state}
    )
    assert set(happy.keys()) == expected_keys

    # Unknown-quarter degraded path
    unknown = read_billing_rate.invoke(
        {"engagement_id": "eng-1", "control_id": "DC-9", "quarter": "Q1", "state": state}
    )
    assert set(unknown.keys()) == expected_keys


def test_read_billing_rate_unit_is_always_basis_points() -> None:
    """DC-9.D billing rates are always bps per the master plan;
    hardcoded in the tool. If a future control wires its rate in a
    different unit, the unit lifts into a per-control map at that
    time — this test guards against silently flipping the unit."""
    current = _evidence(quarter="Q3")
    prior = _evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    for q in ("Q1", "Q2", "Q3", "Q4"):
        result = read_billing_rate.invoke(
            {"engagement_id": "eng-1", "control_id": "DC-9", "quarter": q, "state": state}
        )
        assert result["rate_unit"] == "basis_points"
