"""End-to-end tool-call integration tests for the Extraction agent
(Step 8 task_04).

What's NEW here vs the existing slow tests
------------------------------------------

``tests/integration/test_layer3_extraction_agent_e2e.py`` proves the
agent's plumbing — that it builds, authenticates, runs a ReAct loop,
and returns a structured response. It doesn't pin tool CALL behaviour
because the placeholder tools always returned empty payloads, so any
specific assertion ("agent saw rate=28.5") would have been meaningless.

This file exists because **Step 8 swapped the tool bodies for real
ones** (tasks 01-03). Synthetic-but-realistic evidence flowing
through the InjectedState binding should now make the agent's
final ``ExtractionFindings`` reflect the test data: rates the
fixtures set, amendment text the fixtures embed, variance
explanations the fixtures attach to the DC-2.B notes.

What this file verifies
-----------------------

1. **DC-9.D billing-rate-change scenario** — current quarter has
   ``extracted_value=30.0`` + an IMA amendment fixture in notes;
   prior quarter has ``extracted_value=28.5``. Assertions:
     - Agent calls ``compare_billing_rates`` OR ``read_billing_rate``
       at least once (the LLM picks one or both — trajectory
       non-determinism we documented in Step 7 task_04 means we don't
       pin which).
     - Final ``ExtractionFindings.ima_amendment_found`` is True.
     - ``old_rate`` and ``new_rate`` resolve to 28.5 and 30.0.
2. **DC-2.B variance-plausibility scenario** — synthetic evidence
   with a reviewer note containing a mandate-change explanation.
   Assertions:
     - Agent calls ``read_reviewer_comments`` at least once.
     - Final ``ExtractionFindings.variance_explanation_found`` is True.

Marked ``@pytest.mark.slow`` and gated on ``AZURE_OPENAI_ENDPOINT``.
Cost per file: ~$0.05. Idempotent — safe to re-run.

Run:

    export AZURE_OPENAI_ENDPOINT=https://aoai-aaf-rbpal-dev.openai.azure.com/
    poetry run pytest -m slow tests/integration/test_layer3_tool_calls_e2e.py -v -s

Trajectory non-determinism caveat: even at ``temperature=0`` the
LLM's tool-call order varies run-to-run (documented in
``privateDocs/step_07_layer3_multiagent.md`` task_04). Tests assert
on final ``ExtractionFindings`` shape + minimum tool-call counts,
NOT on exact call counts or order. If a future run produces a
``False`` amendment finding despite the fixture carrying one, the
likely culprit is prompt drift or a LangGraph version bump; capture
the failing trace via ``-v -s`` before triaging.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pytest

from agentic_audit.layer3_agents.extraction_agent import ExtractionAgent
from agentic_audit.layer3_agents.state import ExtractionFindings, InvestigationState
from agentic_audit.models.evidence import (
    ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)

pytestmark = pytest.mark.slow

UTC_TS = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)


def _have_azure_openai_endpoint() -> bool:
    return bool(os.getenv("AZURE_OPENAI_ENDPOINT"))


@pytest.fixture(scope="module")
def extraction_agent() -> ExtractionAgent:
    """Build an ExtractionAgent from env vars, skipping if the
    endpoint isn't configured. Module-scoped fixture so both tests
    share one client (cuts auth overhead in half)."""
    if not _have_azure_openai_endpoint():
        pytest.skip(
            "AZURE_OPENAI_ENDPOINT not set; skipping live integration test. "
            "Export e.g. AZURE_OPENAI_ENDPOINT=https://aoai-aaf-rbpal-dev.openai.azure.com/ "
            "and ensure `az login` is active."
        )
    return ExtractionAgent.from_env()


def _signoff() -> SignOff:
    return SignOff(initials="JD", role="preparer", date=UTC_TS)


def _reviewer() -> SignOff:
    return SignOff(initials="MR", role="reviewer", date=UTC_TS)


def _dc9_evidence(
    *,
    quarter: str,
    rate: float,
    notes: str | None = None,
    cell_refs: list[str] | None = None,
) -> ExtractedEvidence:
    """Build a DC-9 evidence row with the DC-9.D attribute carrying the
    given billing rate. Other DC-9 attributes get default pass-status
    entries so the per-attribute count validator is satisfied."""
    attrs: list[AttributeCheck] = []
    for attr_id in ATTRIBUTES_PER_CONTROL["DC-9"]:
        if attr_id == "D":
            attrs.append(
                AttributeCheck(
                    control_id="DC-9",
                    attribute_id="D",
                    status="pass",
                    evidence_cell_refs=cell_refs or [],
                    extracted_value=rate,
                    notes=notes,
                )
            )
        else:
            attrs.append(
                AttributeCheck(
                    control_id="DC-9",
                    attribute_id=attr_id,  # type: ignore[arg-type]
                    status="pass",
                )
            )
    return ExtractedEvidence(
        engagement_id="eng-tool-e2e",
        control_id="DC-9",
        quarter=quarter,  # type: ignore[arg-type]
        run_id=f"run-{quarter}",
        extraction_timestamp=UTC_TS,
        preparer=_signoff(),
        reviewer=_reviewer(),
        attributes=attrs,
        source_bronze_file_hash="abc",
    )


def _dc2_evidence(
    *,
    quarter: str,
    variance_magnitude: float,
    explanation_notes: str | None = None,
    cell_refs: list[str] | None = None,
) -> ExtractedEvidence:
    """Build a DC-2 evidence row with the DC-2.B attribute carrying the
    given variance magnitude + explanation notes."""
    attrs: list[AttributeCheck] = []
    for attr_id in ATTRIBUTES_PER_CONTROL["DC-2"]:
        if attr_id == "B":
            attrs.append(
                AttributeCheck(
                    control_id="DC-2",
                    attribute_id="B",
                    status="pass",
                    evidence_cell_refs=cell_refs or [],
                    extracted_value=variance_magnitude,
                    notes=explanation_notes,
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
        engagement_id="eng-tool-e2e",
        control_id="DC-2",
        quarter=quarter,  # type: ignore[arg-type]
        run_id=f"run-{quarter}",
        extraction_timestamp=UTC_TS,
        preparer=_signoff(),
        reviewer=_reviewer(),
        attributes=attrs,
        source_bronze_file_hash="abc",
    )


def _billing_rate_change_state(
    current: ExtractedEvidence, prior: ExtractedEvidence
) -> InvestigationState:
    return {
        "investigation_run_id": "inv-tool-e2e-billing",
        "agent_run_id": "sweep-tool-e2e",
        "engagement_id": "eng-tool-e2e",
        "control_id": "DC-9",
        "attribute_id": "D",
        "quarter": "Q3",
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
        "iterations_used": 1,
        "status": "investigating",
    }


def _variance_state(current: ExtractedEvidence, prior: ExtractedEvidence) -> InvestigationState:
    return {
        "investigation_run_id": "inv-tool-e2e-variance",
        "agent_run_id": "sweep-tool-e2e",
        "engagement_id": "eng-tool-e2e",
        "control_id": "DC-2",
        "attribute_id": "B",
        "quarter": "Q3",
        "exception_type": "variance_plausibility",
        "current_quarter_evidence": current,
        "prior_quarter_evidence": prior,
        "investigation_log": [],
        "extraction_findings": None,
        "validation_findings": None,
        "final_narrative": None,
        "judge_verdict": None,
        "judge_confidence": None,
        "confidence_score": 0.0,
        "iterations_used": 1,
        "status": "investigating",
    }


def _print_tool_call_chain(messages: list[Any]) -> dict[str, int]:
    """Walk the agent's message history and print + count tool calls
    by name. Returns the count dict so tests can assert on it."""
    counts: dict[str, int] = {}
    print("\n=== Agent message history ===")
    for i, msg in enumerate(messages):
        msg_type = type(msg).__name__
        content = (getattr(msg, "content", "") or "").strip()
        tool_calls = getattr(msg, "tool_calls", None) or []
        snippet = content if len(content) <= 200 else content[:200] + "..."
        print(f"\n[{i}] {msg_type}")
        if snippet:
            print(f"    content: {snippet}")
        for tc in tool_calls:
            tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "?")
            tc_args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            counts[tc_name] = counts.get(tc_name, 0) + 1
            print(f"    tool_call: {tc_name}({tc_args})")
    print(f"\n=== Tool-call counts: {counts} ===\n")
    return counts


# ── DC-9.D billing-rate-change scenario ──────────────────────────────


def test_dc9d_billing_rate_change_extracts_real_rates_and_amendment(
    extraction_agent: ExtractionAgent,
) -> None:
    """Synthetic-but-realistic DC-9.D evidence with current=30.0,
    prior=28.5, IMA amendment in current notes. Expected outcome:
    agent calls at least one billing-tool, populates old_rate +
    new_rate from real evidence, surfaces ima_amendment_found=True.

    Closes the "rate delta of unknown -> unknown" gap from PR #100
    for the synthetic-evidence case; task_05 then exercises the same
    against real silver."""
    current = _dc9_evidence(
        quarter="Q3",
        rate=30.0,
        notes=(
            "Q3 rate per IMA amendment dated 2026-06-15: management "
            "fee shall be 30.0 bps effective Q3 2026."
        ),
        cell_refs=["sheet1!A12", "amendment.pdf!p2"],
    )
    prior = _dc9_evidence(
        quarter="Q2",
        rate=28.5,
        cell_refs=["sheet1!A11"],
    )
    state = _billing_rate_change_state(current=current, prior=prior)

    findings, messages = extraction_agent._invoke_with_messages(state)
    counts = _print_tool_call_chain(messages)

    print("\n=== Final ExtractionFindings ===")
    print(findings.model_dump_json(indent=2, exclude_none=True))
    print()

    # Tool-call expectations: the LLM should hit at least one of the
    # two billing tools. Trajectory varies (Step 7 task_04 documented
    # the non-determinism), so we assert "≥1" not "exactly N".
    billing_tool_calls = counts.get("read_billing_rate", 0) + counts.get("compare_billing_rates", 0)
    assert billing_tool_calls >= 1, f"agent did not call any billing tool; counts={counts}"

    # Final-findings shape — the load-bearing assertions.
    assert isinstance(findings, ExtractionFindings)
    assert findings.ima_amendment_found is True, (
        f"expected ima_amendment_found=True (amendment fixture in notes); got {findings!r}"
    )
    # Rates should match the fixture. Pydantic preserves None vs float
    # so the assertion is exact-match-or-None.
    assert findings.old_rate == pytest.approx(28.5), (
        f"expected old_rate≈28.5 (prior fixture); got {findings.old_rate}"
    )
    assert findings.new_rate == pytest.approx(30.0), (
        f"expected new_rate≈30.0 (current fixture); got {findings.new_rate}"
    )


# ── DC-2.B variance-plausibility scenario ────────────────────────────


def test_dc2b_variance_plausibility_extracts_explanation_from_notes(
    extraction_agent: ExtractionAgent,
) -> None:
    """Synthetic DC-2.B evidence with variance=0.42 + reviewer notes
    carrying a mandate-change explanation. Expected outcome: agent
    calls read_reviewer_comments at least once and surfaces
    variance_explanation_found=True with the explanation text."""
    current = _dc2_evidence(
        quarter="Q3",
        variance_magnitude=0.42,
        explanation_notes=(
            "Q3 2026 variance of 42% driven by mandate change: client's "
            "IPS amended to add a $120M emerging-markets sleeve, "
            "increasing AUM by 38%. Note dated 2026-09-30."
        ),
        cell_refs=["sheet2!B7", "ips_amendment.pdf!p4"],
    )
    prior = _dc2_evidence(
        quarter="Q2",
        variance_magnitude=0.05,
        cell_refs=["sheet2!B6"],
    )
    state = _variance_state(current=current, prior=prior)

    findings, messages = extraction_agent._invoke_with_messages(state)
    counts = _print_tool_call_chain(messages)

    print("\n=== Final ExtractionFindings ===")
    print(findings.model_dump_json(indent=2, exclude_none=True))
    print()

    # The variance path's tool of choice is read_reviewer_comments
    # (the only tool that surfaces explanation notes). The prompt's
    # tool-use guidance for the variance path explicitly steers the
    # agent here, so this assertion is tighter than the billing case.
    assert counts.get("read_reviewer_comments", 0) >= 1, (
        f"agent did not call read_reviewer_comments; counts={counts}"
    )

    assert isinstance(findings, ExtractionFindings)
    assert findings.variance_explanation_found is True, (
        f"expected variance_explanation_found=True (explanation in notes); got {findings!r}"
    )
    # The explanation text should be present (the agent may paraphrase,
    # so we don't assert verbatim match — just non-empty).
    assert findings.variance_explanation_text, (
        f"expected non-empty variance_explanation_text; got {findings.variance_explanation_text!r}"
    )
