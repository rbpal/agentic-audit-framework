"""End-to-end supervisor-loop integration tests (Step 7 task_07).

These tests exercise ``run_investigation`` against the full
StateGraph with all four sub-agent / judge slots, but use mock
in-memory agents (no LLM, no network). The goal is to pin the
supervisor's terminal-state behaviour:

1. **Iteration cap fires** when sub-agents fail to populate findings
   across MAX_ITERATIONS visits → ``status="escalated_to_human"``
   AND ``final_narrative`` is the canned degraded-escalation
   narrative (the privateDocs § task_07 "every terminal state writes
   a complete row" guarantee).
2. **Judge gate blocks low-quality narratives** — even with all 3
   findings populated and confidence above the threshold, a
   ``judge_verdict="fail"`` routes to escalate. The narrative the
   narrative-agent produced is preserved in ``final_narrative``
   (it's evidence the human reviewer should see), NOT replaced by
   the canned degraded text.
3. **Happy path concludes in ≤ 3 iterations** — DC-9.D Q3 scenario
   with all 3 findings populated + judge=pass + confidence ≥ 0.7
   produces ``status="concluded"`` and
   ``recommendation="ACCEPT"``.

The privateDocs spec for task_07 puts these tests in the unit-test
package; placing under tests/integration here because they exercise
the full LangGraph compile + multi-node loop, even though they don't
hit any external system. CI runs them on every push (no
@pytest.mark.slow gate — they're milliseconds, not minutes).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agentic_audit.layer3_agents.state import (
    ExceptionNarrative,
    ExtractionFindings,
    InvestigationState,
    ValidationFindings,
)
from agentic_audit.layer3_agents.supervisor import (
    MAX_ITERATIONS,
    run_investigation,
)
from agentic_audit.models.evidence import (
    ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)
from agentic_audit.models.judge import JudgeResponse

UTC_TS = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


# ── Fixtures ─────────────────────────────────────────────────────────


def _evidence(control_id: str, quarter: str) -> ExtractedEvidence:
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


class _FakeExtractionAgent:
    def __init__(self, findings: ExtractionFindings) -> None:
        self._findings = findings
        self.call_count = 0

    def invoke(self, state: InvestigationState) -> ExtractionFindings:
        self.call_count += 1
        return self._findings


class _FakeValidationAgent:
    def __init__(self, findings: ValidationFindings) -> None:
        self._findings = findings
        self.call_count = 0

    def invoke(self, state: InvestigationState) -> ValidationFindings:
        self.call_count += 1
        return self._findings


class _FakeNarrativeAgent:
    def __init__(self, narrative: ExceptionNarrative) -> None:
        self._narrative = narrative
        self.call_count = 0

    def invoke(self, state: InvestigationState) -> ExceptionNarrative:
        self.call_count += 1
        return self._narrative


def _judge_pass(state: InvestigationState) -> JudgeResponse:
    return JudgeResponse(
        verdict="pass",
        confidence=0.92,
        reasoning="judge approves",
        cited_evidence_fields=["extraction_findings"],
    )


def _judge_fail(state: InvestigationState) -> JudgeResponse:
    return JudgeResponse(
        verdict="fail",
        confidence=0.85,
        reasoning="judge rejects",
        cited_evidence_fields=["extraction_findings"],
    )


def _dc9d_check() -> AttributeCheck:
    return AttributeCheck(control_id="DC-9", attribute_id="D", status="fail")


def _ok_extraction() -> ExtractionFindings:
    return ExtractionFindings(
        confidence=0.92,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_text="Amendment effective Q3 2026 authorising 30.0 bps.",
        evidence_anchors=["sheet1!A12"],
    )


def _ok_validation() -> ValidationFindings:
    return ValidationFindings(
        is_authorized=True,
        confidence=0.92,
        reasoning="Amendment authorises rate change.",
    )


def _ok_narrative() -> ExceptionNarrative:
    return ExceptionNarrative(
        narrative_text="Rate moved from 28.5 to 30.0 per the amendment Effective Q3 2026.",
        citations=["sheet1!A12"],
        recommendation="ACCEPT",
        word_count=14,
    )


# ── Test 1: iteration cap fires + degraded narrative ─────────────────


def test_iteration_cap_escalates_after_3_iterations_with_degraded_narrative() -> None:
    """Per privateDocs § task_07 spec test 1: validation never
    populates findings → after MAX_ITERATIONS sub-agent visits, the
    supervisor routes to escalate.

    The privateDocs spec uses "validation returns None" wording; we
    achieve the same outcome by leaving validation_agent un-injected
    (the no-op sub-agent node returns no validation_findings, which
    keeps the supervisor routing back to validation indefinitely).

    Verifies the task_07 "never silent failure" guarantee: even on
    this early-escalate path where the narrative agent never ran,
    the final state carries a complete ``ExceptionNarrative`` (the
    canned degraded escalation text) so ``gold.layer3_decisions``
    still gets a complete row."""
    extraction_agent = _FakeExtractionAgent(_ok_extraction())
    # No validation_agent injected — its no-op node will never
    # populate validation_findings, so supervisor keeps routing to
    # validation until the cap fires.

    result = run_investigation(
        check=_dc9d_check(),
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-iter-cap",
        extraction_agent=extraction_agent,  # type: ignore[arg-type]
        # validation_agent absent
        # narrative_agent absent
        # judge absent
    )

    assert result["status"] == "escalated_to_human"
    assert result["iterations_used"] == MAX_ITERATIONS

    # Degraded escalation narrative invariant — every terminal
    # state writes a complete row.
    final = result.get("final_narrative")
    assert final is not None
    assert final.recommendation == "ESCALATE"
    assert final.citations == []
    assert "human review required" in final.narrative_text


def test_iteration_cap_with_no_agents_at_all_still_emits_degraded_narrative() -> None:
    """Belt-and-braces variant: NO agents wired (extraction also
    no-ops). Same guarantee — escalate + degraded narrative."""
    result = run_investigation(
        check=_dc9d_check(),
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-no-agents",
    )

    assert result["status"] == "escalated_to_human"
    assert result["iterations_used"] == MAX_ITERATIONS
    final = result.get("final_narrative")
    assert final is not None
    assert final.recommendation == "ESCALATE"
    assert "human review required" in final.narrative_text


# ── Test 2: judge gate blocks low-quality narrative ──────────────────


def test_judge_gate_blocks_low_quality_narrative_and_escalates() -> None:
    """Per privateDocs § task_07 spec test 2: all three sub-agent
    outputs landed cleanly (high-confidence narrative + happy
    findings) BUT the judge returns ``verdict="fail"``. The
    supervisor's believe-either-fail gate routes to escalate.

    Verifies that the LLM-generated narrative IS preserved in the
    final state (it's evidence the human reviewer needs), NOT
    replaced by the canned degraded text — ``_ensure_terminal_narrative``
    only fires when ``final_narrative`` is None."""
    narrative = _ok_narrative()

    result = run_investigation(
        check=_dc9d_check(),
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-judge-fail",
        extraction_agent=_FakeExtractionAgent(_ok_extraction()),  # type: ignore[arg-type]
        validation_agent=_FakeValidationAgent(_ok_validation()),  # type: ignore[arg-type]
        narrative_agent=_FakeNarrativeAgent(narrative),  # type: ignore[arg-type]
        judge=_judge_fail,
    )

    assert result["status"] == "escalated_to_human"
    assert result["judge_verdict"] == "fail"
    # Narrative preserved (NOT overwritten by degraded text).
    final = result.get("final_narrative")
    assert final is not None
    assert final is narrative
    assert final.recommendation == "ACCEPT"  # narrative-agent's verdict, not the gate's
    assert "28.5" in final.narrative_text  # the real LLM-generated text, not canned


# ── Test 3: happy path concludes ─────────────────────────────────────


def test_happy_path_concludes_in_three_iterations() -> None:
    """Per privateDocs § task_07 spec test 3: DC-9.D Q3 scenario with
    all 3 findings populated + judge=pass + confidence ≥
    CONFIDENCE_THRESHOLD produces ``status="concluded"`` with
    ``recommendation="ACCEPT"`` in ≤ MAX_ITERATIONS sub-agent
    iterations.

    Each sub-agent is invoked exactly once. The supervisor's 4th
    visit (the conclude-decision visit) does NOT count as an
    iteration — that's the cap-semantics fix landed in task_06."""
    extraction = _FakeExtractionAgent(_ok_extraction())
    validation = _FakeValidationAgent(_ok_validation())
    narrative = _FakeNarrativeAgent(_ok_narrative())

    result = run_investigation(
        check=_dc9d_check(),
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-happy",
        extraction_agent=extraction,  # type: ignore[arg-type]
        validation_agent=validation,  # type: ignore[arg-type]
        narrative_agent=narrative,  # type: ignore[arg-type]
        judge=_judge_pass,
    )

    assert result["status"] == "concluded"
    assert result["iterations_used"] == MAX_ITERATIONS  # exactly 3
    assert extraction.call_count == 1
    assert validation.call_count == 1
    assert narrative.call_count == 1

    final = result.get("final_narrative")
    assert final is not None
    assert final.recommendation == "ACCEPT"

    # Judge verdict + confidence threaded into state for persistence.
    assert result["judge_verdict"] == "pass"
    assert result["confidence_score"] >= 0.7  # validation.confidence


def test_happy_path_concludes_for_variance_plausibility() -> None:
    """Symmetric happy path on the DC-2.B variance branch — same
    contract, different scope."""
    extraction = ExtractionFindings(
        confidence=0.88,
        variance_explanation_found=True,
        variance_magnitude=0.42,
        variance_explanation_text="Q3 mandate change explains the 42% variance.",
        evidence_anchors=["sheet2!B7"],
    )
    validation = ValidationFindings(
        is_authorized=True,
        confidence=0.85,
        reasoning="Mandate change quantitatively matches the magnitude.",
    )
    narrative = ExceptionNarrative(
        narrative_text="Variance of 0.42 explained by Q3 mandate change.",
        citations=["sheet2!B7"],
        recommendation="ACCEPT",
        word_count=10,
    )

    result = run_investigation(
        check=AttributeCheck(control_id="DC-2", attribute_id="B", status="fail"),
        current=_evidence("DC-2", "Q3"),
        prior=_evidence("DC-2", "Q2"),
        agent_run_id="sweep-variance-happy",
        extraction_agent=_FakeExtractionAgent(extraction),  # type: ignore[arg-type]
        validation_agent=_FakeValidationAgent(validation),  # type: ignore[arg-type]
        narrative_agent=_FakeNarrativeAgent(narrative),  # type: ignore[arg-type]
        judge=_judge_pass,
    )

    assert result["status"] == "concluded"
    assert result["final_narrative"] is not None
    assert result["final_narrative"].recommendation == "ACCEPT"  # type: ignore[union-attr]


# ── Bonus: confident-negative validation concludes (with ESCALATE) ───


def test_confident_negative_validation_concludes_with_escalate_recommendation() -> None:
    """Edge case worth pinning: validation says ``is_authorized=False``
    with HIGH confidence (e.g. fast-path no-document at 0.9). The
    supervisor concludes (because the narrative is trustworthy) but
    the ``ExceptionNarrative.recommendation`` is ESCALATE — this is
    the "automation produced a confident negative answer, please
    review" case, structurally distinct from "automation could not
    answer at all".

    Both terminal states write a complete row, but a downstream
    consumer reading ``status`` + ``recommendation`` together can
    distinguish them: ``status=concluded``+``recommendation=ESCALATE``
    means "automation finished, recommends human action";
    ``status=escalated_to_human`` means "automation could not finish"."""
    confident_negative_validation = ValidationFindings(
        is_authorized=False,
        confidence=0.9,
        reasoning="No supporting document found.",
    )
    escalate_narrative = ExceptionNarrative(
        narrative_text=(
            "Rate change from 28.5 to 30.0 detected; no IMA amendment "
            "located in evidence. Recommend human follow-up."
        ),
        citations=["sheet1!A12"],
        recommendation="ESCALATE",
        word_count=20,
    )

    result = run_investigation(
        check=_dc9d_check(),
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-confident-negative",
        extraction_agent=_FakeExtractionAgent(_ok_extraction()),  # type: ignore[arg-type]
        validation_agent=_FakeValidationAgent(confident_negative_validation),  # type: ignore[arg-type]
        narrative_agent=_FakeNarrativeAgent(escalate_narrative),  # type: ignore[arg-type]
        judge=_judge_pass,
    )

    # Concluded (automation finished), but recommendation is ESCALATE
    # (the answer is "this needs human review")
    assert result["status"] == "concluded"
    final = result.get("final_narrative")
    assert final is not None
    assert final.recommendation == "ESCALATE"
    # Real narrative preserved, not the canned degraded text
    assert "28.5" in final.narrative_text


# ── Sanity: degraded narrative not applied to concluded states ───────


def test_concluded_state_keeps_real_narrative_not_degraded() -> None:
    """``_ensure_terminal_narrative`` must be a no-op on concluded
    paths — the narrative agent's output is the answer the human
    reviewer (or downstream consumer) acts on. Wrapping it with the
    canned degraded text would silently destroy the LLM's verdict."""
    real_narrative = _ok_narrative()
    result = run_investigation(
        check=_dc9d_check(),
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-noop-on-conclude",
        extraction_agent=_FakeExtractionAgent(_ok_extraction()),  # type: ignore[arg-type]
        validation_agent=_FakeValidationAgent(_ok_validation()),  # type: ignore[arg-type]
        narrative_agent=_FakeNarrativeAgent(real_narrative),  # type: ignore[arg-type]
        judge=_judge_pass,
    )

    assert result["status"] == "concluded"
    final = result.get("final_narrative")
    assert final is not None
    assert final is real_narrative
    # Definitely NOT the degraded text
    assert "human review required" not in final.narrative_text


# ── Helper-level unit test: degraded narrative shape ─────────────────


def test_degraded_narrative_shape_matches_spec() -> None:
    """The canned text + recommendation + empty citations are an
    exact-match contract with downstream consumers (the human-review
    UI parses ``recommendation == "ESCALATE"`` AND
    ``citations == []`` together to detect this branch). Pin them."""
    from agentic_audit.layer3_agents.supervisor import (
        _build_degraded_escalation_narrative,
    )

    narrative = _build_degraded_escalation_narrative()
    assert narrative.recommendation == "ESCALATE"
    assert narrative.citations == []
    assert narrative.narrative_text == (
        "Automated investigation could not reach sufficient confidence; human review required."
    )
    assert narrative.word_count == 10


def test_run_investigation_preserves_final_narrative_when_already_set(
    monkeypatch: Any,
) -> None:
    """Defensive: if a future refactor accidentally calls
    ``_ensure_terminal_narrative`` on an already-narrative-bearing
    escalate, it must not overwrite. (The judge-fail path tests this
    end-to-end, but pinning the helper directly keeps the contract
    visible at the unit level.)"""
    from agentic_audit.layer3_agents.supervisor import _ensure_terminal_narrative

    real_narrative = _ok_narrative()
    state: InvestigationState = {  # type: ignore[typeddict-item]
        "status": "escalated_to_human",
        "final_narrative": real_narrative,
        "investigation_log": [],
        "iterations_used": 3,
    }
    out = _ensure_terminal_narrative(state)
    assert out["final_narrative"] is real_narrative


def test_ensure_terminal_narrative_noop_on_investigating_status() -> None:
    """If the status is anything other than ``escalated_to_human``,
    the helper must not touch the state."""
    from agentic_audit.layer3_agents.supervisor import _ensure_terminal_narrative

    state: InvestigationState = {  # type: ignore[typeddict-item]
        "status": "concluded",
        "final_narrative": None,  # bizarre but possible
        "investigation_log": [],
        "iterations_used": 3,
    }
    out = _ensure_terminal_narrative(state)
    # Concluded states are not the degraded-narrative branch — even
    # if final_narrative is None, leave it alone (caller's bug to
    # surface explicitly, not silently mask).
    assert out.get("final_narrative") is None
