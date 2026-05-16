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
    _detect_amendment_in_notes,
    _find_attribute_check,
    _resolve_evidence_for_quarter,
    compare_billing_rates,
    read_billing_rate,
    read_reviewer_comments,
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
        # Real Layer-1 silver shape for DC-9.D — discovered by Step 8
        # task_05 live verification. Layer 1 stores a dict, not a
        # bare numeric; tool projects current_rate from the dict.
        ({"current_rate": 0.005, "prior_rate": 0.005, "rate_change": False}, 0.005),
        ({"current_rate": 30.0, "prior_rate": 28.5, "rate_change": True}, 30.0),
        # Dict with current_rate as a string — same string-coercion
        # behaviour applies after dict-unwrap.
        ({"current_rate": "0.005"}, 0.005),
        # Dict missing current_rate → None (degraded, not raised).
        ({"prior_rate": 0.005, "rate_change": False}, None),
        # Dict with current_rate=None → None.
        ({"current_rate": None, "prior_rate": 0.005}, None),
    ],
)
def test_coerce_rate_handles_typed_stringified_and_dict_shapes(
    value: Any, expected: float | None
) -> None:
    """Four input shapes from Layer-1 silver:
    - bare numeric (synthetic tests / future Layer-1 shape)
    - stringified numeric (cell-type-dependent serialisation)
    - dict with current_rate key (real Layer-1 DC-9.D shape)
    - None / unrecognised → None (never raise; ReAct loop continues
      with rate=None signal)
    """
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


# ── _detect_amendment_in_notes (Step 8 task_02 helper) ───────────────


@pytest.mark.parametrize(
    "notes,expected_match",
    [
        ("Amendment dated 2026-06-15 authorises 30.0 bps.", True),
        ("Per IMA amendment, rate increased.", True),
        ("Side letter referencing fee schedule update.", True),
        ("Addendum to the Investment Management Agreement.", True),
        ("ima amendment lowercase variant", True),  # case-insensitive
        ("AMENDMENT in shouty caps", True),
        ("Reviewer concurs; standard fee schedule.", False),
        ("No comment provided.", False),
        ("", False),
        (None, False),
    ],
)
def test_detect_amendment_in_notes_case_insensitive_substring_match(
    notes: str | None, expected_match: bool
) -> None:
    """Helper accepts the four marker substrings case-insensitively
    and returns the notes verbatim on hit. Empty/None/non-matching
    notes degrade to None."""
    result = _detect_amendment_in_notes(notes)
    if expected_match:
        assert result == notes
    else:
        assert result is None


# ── compare_billing_rates — rate math ────────────────────────────────


def test_compare_billing_rates_both_present_computes_delta_and_percent() -> None:
    """Standard DC-9.D Q3 vs Q2 lookup: numeric rates on both
    quarters, delta = current - prior, percent_change = delta /
    prior. Five rate fields all populated."""
    current = _evidence(quarter="Q3", dc9d_rate=30.0)
    prior = _evidence(quarter="Q2", dc9d_rate=28.5)
    state = _state(current=current, prior=prior)

    result = compare_billing_rates.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "current_quarter": "Q3",
            "prior_quarter": "Q2",
            "state": state,
        }
    )

    assert result["current_rate"] == 30.0
    assert result["prior_rate"] == 28.5
    assert result["delta"] == pytest.approx(1.5)
    assert result["percent_change"] == pytest.approx(1.5 / 28.5)


def test_compare_billing_rates_prior_missing_degrades_delta_to_null() -> None:
    """Prior quarter not in state — delta + percent_change both None,
    but current_rate still populated."""
    current = _evidence(quarter="Q3", dc9d_rate=30.0)
    prior = _evidence(quarter="Q2", dc9d_rate=28.5)
    state = _state(current=current, prior=prior)

    result = compare_billing_rates.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "current_quarter": "Q3",
            "prior_quarter": "Q1",  # not in state
            "state": state,
        }
    )

    assert result["current_rate"] == 30.0
    assert result["prior_rate"] is None
    assert result["delta"] is None
    assert result["percent_change"] is None


def test_compare_billing_rates_current_missing_degrades_to_null() -> None:
    current = _evidence(quarter="Q3", dc9d_rate=30.0)
    prior = _evidence(quarter="Q2", dc9d_rate=28.5)
    state = _state(current=current, prior=prior)

    result = compare_billing_rates.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "current_quarter": "Q4",  # not in state
            "prior_quarter": "Q2",
            "state": state,
        }
    )

    assert result["current_rate"] is None
    assert result["prior_rate"] == 28.5
    assert result["delta"] is None
    assert result["percent_change"] is None


def test_compare_billing_rates_zero_prior_yields_null_percent_change() -> None:
    """Defensive: prior_rate=0 would divide-by-zero on percent_change.
    delta still computable (current - 0 = current), but percent_change
    is None to avoid math errors AND avoid surfacing infinity/NaN
    into the agent's downstream reasoning."""
    current = _evidence(quarter="Q3", dc9d_rate=30.0)
    prior = _evidence(quarter="Q2", dc9d_rate=0.0)
    state = _state(current=current, prior=prior)

    result = compare_billing_rates.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "current_quarter": "Q3",
            "prior_quarter": "Q2",
            "state": state,
        }
    )

    assert result["current_rate"] == 30.0
    assert result["prior_rate"] == 0.0
    assert result["delta"] == 30.0  # still computed
    assert result["percent_change"] is None  # no division by zero


def test_compare_billing_rates_negative_prior_yields_null_percent_change() -> None:
    """Billing rates shouldn't be negative; if Layer-1 extracted one
    by mistake, percent_change degrades to None rather than surfacing
    a sign-confused ratio that would mislead the agent."""
    current = _evidence(quarter="Q3", dc9d_rate=30.0)
    prior = _evidence(quarter="Q2", dc9d_rate=-5.0)
    state = _state(current=current, prior=prior)

    result = compare_billing_rates.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "current_quarter": "Q3",
            "prior_quarter": "Q2",
            "state": state,
        }
    )

    assert result["percent_change"] is None
    assert result["delta"] == 35.0  # current - (-5) = 35


def test_compare_billing_rates_non_numeric_extracted_value_degrades() -> None:
    """Layer-1 stored non-numeric in current quarter's rate slot:
    rate=None propagates through delta + percent_change."""
    current = _evidence(quarter="Q3", dc9d_rate="see attached")
    prior = _evidence(quarter="Q2", dc9d_rate=28.5)
    state = _state(current=current, prior=prior)

    result = compare_billing_rates.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "current_quarter": "Q3",
            "prior_quarter": "Q2",
            "state": state,
        }
    )

    assert result["current_rate"] is None
    assert result["prior_rate"] == 28.5
    assert result["delta"] is None
    assert result["percent_change"] is None


# ── compare_billing_rates — amendment surface ────────────────────────


def test_compare_billing_rates_amendment_present_in_current_notes_is_surfaced() -> None:
    """Amendment marker in current quarter's DC-9.D notes -> full
    ima_amendment_* triple populated with notes verbatim + first
    cell ref as the lineage anchor."""
    current = _evidence(
        quarter="Q3",
        dc9d_rate=30.0,
        dc9d_notes="Amendment dated 2026-06-15 authorises 30.0 bps effective Q3.",
        dc9d_cell_refs=["sheet1!A12", "amendment.pdf!p2"],
    )
    prior = _evidence(quarter="Q2", dc9d_rate=28.5)
    state = _state(current=current, prior=prior)

    result = compare_billing_rates.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "current_quarter": "Q3",
            "prior_quarter": "Q2",
            "state": state,
        }
    )

    assert result["ima_amendment_found"] is True
    assert "Amendment dated 2026-06-15" in result["ima_amendment_text"]
    assert result["ima_amendment_cell_ref"] == "sheet1!A12"


def test_compare_billing_rates_amendment_absent_when_notes_unstructured() -> None:
    """Notes that don't carry an amendment marker -> ima_amendment_*
    all empty/False. Common case when the reviewer just attached a
    standard sign-off note."""
    current = _evidence(
        quarter="Q3",
        dc9d_rate=30.0,
        dc9d_notes="Reviewer concurs; standard fee schedule.",
        dc9d_cell_refs=["sheet1!A12"],
    )
    prior = _evidence(quarter="Q2", dc9d_rate=28.5)
    state = _state(current=current, prior=prior)

    result = compare_billing_rates.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "current_quarter": "Q3",
            "prior_quarter": "Q2",
            "state": state,
        }
    )

    assert result["ima_amendment_found"] is False
    assert result["ima_amendment_text"] == ""
    assert result["ima_amendment_cell_ref"] == ""


def test_compare_billing_rates_amendment_absent_when_notes_none() -> None:
    """No notes attached at all -> same empty amendment shape."""
    current = _evidence(quarter="Q3", dc9d_rate=30.0, dc9d_notes=None)
    prior = _evidence(quarter="Q2", dc9d_rate=28.5)
    state = _state(current=current, prior=prior)

    result = compare_billing_rates.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "current_quarter": "Q3",
            "prior_quarter": "Q2",
            "state": state,
        }
    )

    assert result["ima_amendment_found"] is False
    assert result["ima_amendment_text"] == ""
    assert result["ima_amendment_cell_ref"] == ""


def test_compare_billing_rates_amendment_uses_current_not_prior_notes() -> None:
    """The amendment that justifies a Q3 rate change lives ON the Q3
    evidence (the auditor attaches the reference to the period under
    investigation), not the prior period. If the marker is only in
    the prior period's notes, the tool should NOT surface it."""
    current = _evidence(quarter="Q3", dc9d_rate=30.0, dc9d_notes="Standard quarter.")
    prior = _evidence(
        quarter="Q2",
        dc9d_rate=28.5,
        dc9d_notes="Amendment scheduled for Q3 implementation.",
    )
    state = _state(current=current, prior=prior)

    result = compare_billing_rates.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "current_quarter": "Q3",
            "prior_quarter": "Q2",
            "state": state,
        }
    )

    assert result["ima_amendment_found"] is False


def test_compare_billing_rates_amendment_without_cell_refs_yields_empty_cell_ref() -> None:
    """Amendment marker present but no cell refs on the check -> the
    cell_ref string is empty even though found/text are populated."""
    current = _evidence(
        quarter="Q3",
        dc9d_rate=30.0,
        dc9d_notes="IMA amendment in place.",
        dc9d_cell_refs=[],
    )
    prior = _evidence(quarter="Q2", dc9d_rate=28.5)
    state = _state(current=current, prior=prior)

    result = compare_billing_rates.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "current_quarter": "Q3",
            "prior_quarter": "Q2",
            "state": state,
        }
    )

    assert result["ima_amendment_found"] is True
    assert result["ima_amendment_text"] == "IMA amendment in place."
    assert result["ima_amendment_cell_ref"] == ""


# ── compare_billing_rates — shape invariants ─────────────────────────


def test_compare_billing_rates_always_returns_seven_keys() -> None:
    """The 7-key return shape is part of the agent prompt's contract.
    All paths (happy + degraded) must produce the same key set."""
    current = _evidence(quarter="Q3")
    prior = _evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    expected_keys = {
        "current_rate",
        "prior_rate",
        "delta",
        "percent_change",
        "ima_amendment_found",
        "ima_amendment_text",
        "ima_amendment_cell_ref",
    }

    # Happy path
    happy = compare_billing_rates.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "current_quarter": "Q3",
            "prior_quarter": "Q2",
            "state": state,
        }
    )
    assert set(happy.keys()) == expected_keys

    # Unknown-quarters degraded path
    unknown = compare_billing_rates.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-9",
            "current_quarter": "Q1",
            "prior_quarter": "Q0",
            "state": state,
        }
    )
    assert set(unknown.keys()) == expected_keys
    assert all(
        unknown[k] is None for k in ("current_rate", "prior_rate", "delta", "percent_change")
    )
    assert unknown["ima_amendment_found"] is False


# ── read_reviewer_comments (Step 8 task_03) ──────────────────────────


def _dc2_evidence(
    *,
    quarter: str = "Q3",
    dc2b_variance: Any = 0.42,
    dc2b_notes: str | None = None,
    dc2b_cell_refs: list[str] | None = None,
) -> ExtractedEvidence:
    """Build a synthetic DC-2 ExtractedEvidence with a configurable
    DC-2.B attribute check (the variance-plausibility slot). Other
    DC-2 attributes get default pass-status entries to satisfy the
    per-attribute count validator."""
    attrs: list[AttributeCheck] = []
    for attr_id in ATTRIBUTES_PER_CONTROL["DC-2"]:
        if attr_id == "B":
            attrs.append(
                AttributeCheck(
                    control_id="DC-2",
                    attribute_id="B",
                    status="pass",
                    evidence_cell_refs=dc2b_cell_refs or [],
                    extracted_value=dc2b_variance,
                    notes=dc2b_notes,
                )
            )
        else:
            attrs.append(
                AttributeCheck(
                    control_id="DC-2",
                    attribute_id=attr_id,  # type: ignore[arg-type]
                    status="pass",
                )
            )
    return ExtractedEvidence(
        engagement_id="eng-1",
        control_id="DC-2",
        quarter=quarter,  # type: ignore[arg-type]
        run_id="run-1",
        extraction_timestamp=UTC_TS,
        preparer=SignOff(initials="JD", role="preparer", date=UTC_TS),
        reviewer=SignOff(initials="MR", role="reviewer", date=UTC_TS),
        attributes=attrs,
        source_bronze_file_hash="abc",
    )


def test_read_reviewer_comments_happy_path_variance_explanation_present() -> None:
    """Standard DC-2.B Q3 lookup: notes carry the variance
    explanation, cell refs populated. All four return-shape fields
    populated."""
    current = _dc2_evidence(
        quarter="Q3",
        dc2b_variance=0.42,
        dc2b_notes=(
            "Q3 variance of 42% driven by mandate change adding "
            "$120M emerging-markets sleeve; AUM up 38%."
        ),
        dc2b_cell_refs=["sheet2!B7", "ips_amendment.pdf!p4"],
    )
    prior = _dc2_evidence(quarter="Q2", dc2b_variance=0.05)
    state = _state(current=current, prior=prior)

    result = read_reviewer_comments.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-2",
            "quarter": "Q3",
            "attribute_id": "B",
            "state": state,
        }
    )

    assert result["variance_explanation_found"] is True
    assert "mandate change" in result["variance_explanation_text"]
    assert result["comments"] == [result["variance_explanation_text"]]
    assert result["source_cell_refs"] == ["sheet2!B7", "ips_amendment.pdf!p4"]


def test_read_reviewer_comments_notes_absent_degrades_to_empty() -> None:
    """No notes attached → variance_explanation_found=False, empty
    comments list, empty text. Cell refs may still be present
    (reviewer attached a lineage anchor without prose)."""
    current = _dc2_evidence(
        quarter="Q3",
        dc2b_variance=0.42,
        dc2b_notes=None,
        dc2b_cell_refs=["sheet2!B7"],
    )
    prior = _dc2_evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    result = read_reviewer_comments.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-2",
            "quarter": "Q3",
            "attribute_id": "B",
            "state": state,
        }
    )

    assert result["variance_explanation_found"] is False
    assert result["comments"] == []
    assert result["variance_explanation_text"] == ""
    assert result["source_cell_refs"] == ["sheet2!B7"]


def test_read_reviewer_comments_empty_notes_string_degrades_to_empty() -> None:
    """Notes is an empty string (rather than None) — same shape
    semantics. ``bool("")`` is False so the explanation is treated as
    absent."""
    current = _dc2_evidence(quarter="Q3", dc2b_notes="")
    prior = _dc2_evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    result = read_reviewer_comments.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-2",
            "quarter": "Q3",
            "attribute_id": "B",
            "state": state,
        }
    )

    assert result["variance_explanation_found"] is False
    assert result["comments"] == []


def test_read_reviewer_comments_unknown_quarter_degrades_to_empty() -> None:
    """LLM passed a quarter not loaded into state — returns the empty
    shape rather than raising. Agent loop continues with 'no
    explanation found' signal."""
    current = _dc2_evidence(quarter="Q3", dc2b_notes="explanation text")
    prior = _dc2_evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    result = read_reviewer_comments.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-2",
            "quarter": "Q1",  # not in state
            "attribute_id": "B",
            "state": state,
        }
    )

    assert result["variance_explanation_found"] is False
    assert result["comments"] == []
    assert result["variance_explanation_text"] == ""
    assert result["source_cell_refs"] == []


def test_read_reviewer_comments_unknown_attribute_degrades_to_empty() -> None:
    """LLM passed an attribute_id that's not in the control's
    attribute set (e.g., asked for DC-2.Z which doesn't exist) —
    returns empty shape."""
    current = _dc2_evidence(quarter="Q3", dc2b_notes="something")
    prior = _dc2_evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    result = read_reviewer_comments.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-2",
            "quarter": "Q3",
            "attribute_id": "Z",  # not a real attribute
            "state": state,
        }
    )

    assert result["variance_explanation_found"] is False
    assert result["comments"] == []


def test_read_reviewer_comments_picks_correct_attribute() -> None:
    """The tool projects per-attribute, NOT hardcoded to DC-2.B. A
    DC-2.A query against state should pull DC-2.A's notes (which the
    default fixture leaves None) rather than DC-2.B's. Verifies the
    LLM-passed attribute_id is actually consulted."""
    current = _dc2_evidence(
        quarter="Q3",
        dc2b_notes="DC-2.B variance explanation only",
    )
    prior = _dc2_evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    result = read_reviewer_comments.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-2",
            "quarter": "Q3",
            "attribute_id": "A",  # NOT B — should pull A's empty notes
            "state": state,
        }
    )

    # A's notes are None by default fixture; tool should NOT surface
    # B's notes here.
    assert result["variance_explanation_found"] is False
    assert "DC-2.B variance" not in result["variance_explanation_text"]


def test_read_reviewer_comments_prior_quarter_lookup() -> None:
    """LLM can ask for prior quarter explicitly — should resolve
    against prior_quarter_evidence, not current."""
    current = _dc2_evidence(quarter="Q3", dc2b_notes="Q3 note")
    prior = _dc2_evidence(quarter="Q2", dc2b_notes="Q2 note")
    state = _state(current=current, prior=prior)

    result = read_reviewer_comments.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-2",
            "quarter": "Q2",
            "attribute_id": "B",
            "state": state,
        }
    )

    assert result["variance_explanation_text"] == "Q2 note"


def test_read_reviewer_comments_always_returns_four_keys() -> None:
    """The 4-key return shape is part of the agent prompt's contract.
    Happy + degraded paths must produce the same key set."""
    current = _dc2_evidence(quarter="Q3", dc2b_notes="text")
    prior = _dc2_evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    expected_keys = {
        "comments",
        "variance_explanation_found",
        "variance_explanation_text",
        "source_cell_refs",
    }

    happy = read_reviewer_comments.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-2",
            "quarter": "Q3",
            "attribute_id": "B",
            "state": state,
        }
    )
    assert set(happy.keys()) == expected_keys

    unknown = read_reviewer_comments.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-2",
            "quarter": "Q1",
            "attribute_id": "B",
            "state": state,
        }
    )
    assert set(unknown.keys()) == expected_keys


def test_read_reviewer_comments_source_cell_refs_is_a_copy_not_reference() -> None:
    """Defensive: the tool returns ``list(check.evidence_cell_refs)``
    rather than the underlying list directly, so a caller mutating
    the returned list can't poison the AttributeCheck's lineage data.
    Important since the agent's tool-call history serialises this and
    a leaked mutation would corrupt the trace."""
    current = _dc2_evidence(
        quarter="Q3",
        dc2b_notes="x",
        dc2b_cell_refs=["sheet2!B7"],
    )
    prior = _dc2_evidence(quarter="Q2")
    state = _state(current=current, prior=prior)

    result = read_reviewer_comments.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-2",
            "quarter": "Q3",
            "attribute_id": "B",
            "state": state,
        }
    )
    result["source_cell_refs"].append("mutated-by-caller!")

    # Pull again — the second call should NOT see the mutation
    second = read_reviewer_comments.invoke(
        {
            "engagement_id": "eng-1",
            "control_id": "DC-2",
            "quarter": "Q3",
            "attribute_id": "B",
            "state": state,
        }
    )
    assert second["source_cell_refs"] == ["sheet2!B7"]
