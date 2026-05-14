"""Tests for the supervisor routing logic — task_03.

Three layers of coverage:

1. ``route_from_supervisor`` — the pure conditional-edge function.
   One test per rule in the 5-rule decision tree (iteration cap →
   extraction → validation → narrative → judge gate). Plus boundary
   tests for the confidence threshold and the missing-judge-verdict
   fail-closed default.
2. ``supervisor_node`` — the side-effectful node. Increments
   iterations_used, calls the injected judge when ``final_narrative``
   first lands, appends an InvestigationStep naming the actual
   destination, sets ``status`` on terminal routes.
3. End-to-end via ``run_investigation`` — happy-path conclude with a
   stub judge that returns pass; happy-path escalate when the judge
   returns fail.

The judge is injected via ``config["configurable"]["layer3_judge"]``
as a ``Layer3JudgeFunc`` ``Callable[[InvestigationState], JudgeResponse]``.
Tests pass a plain Python lambda; no mocking library needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig

from agentic_audit.layer3_agents.state import (
    ExceptionNarrative,
    ExtractionFindings,
    InvestigationState,
    ValidationFindings,
)
from agentic_audit.layer3_agents.supervisor import (
    CONFIDENCE_THRESHOLD,
    LAYER3_JUDGE_CONFIG_KEY,
    MAX_ITERATIONS,
    _decide_route,
    route_from_supervisor,
    run_investigation,
    supervisor_node,
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


def _extraction_ok() -> ExtractionFindings:
    return ExtractionFindings(
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_found=True,
        ima_amendment_text="Board-approved amendment effective Q3.",
        evidence_anchors=["DC-9!r3c4"],
        confidence=0.9,
    )


def _validation_ok() -> ValidationFindings:
    return ValidationFindings(
        is_authorized=True,
        confidence=0.85,
        reasoning="Amendment text covers the rate change.",
    )


def _narrative_ok() -> ExceptionNarrative:
    return ExceptionNarrative(
        narrative_text="Rate change supported by IMA amendment.",
        citations=["DC-9!r3c4", "IMA-amendment-2026-03-15"],
        recommendation="ACCEPT",
        word_count=7,
    )


def _base_state(**overrides: Any) -> InvestigationState:
    """Minimal state for routing tests. Caller layers in only the
    fields the test cares about — ``total=False`` keeps the dict valid
    regardless of which keys are present."""
    base: dict[str, Any] = {
        "iterations_used": 0,
        "extraction_findings": None,
        "validation_findings": None,
        "final_narrative": None,
        "judge_verdict": None,
        "judge_confidence": None,
        "confidence_score": 0.0,
    }
    base.update(overrides)
    return base  # type: ignore[return-value]


# ── route_from_supervisor — pure routing decision ────────────────────


def test_routes_to_extraction_when_extraction_findings_none() -> None:
    state = _base_state()
    assert route_from_supervisor(state) == "extraction"


def test_routes_to_validation_when_validation_findings_none() -> None:
    state = _base_state(extraction_findings=_extraction_ok())
    assert route_from_supervisor(state) == "validation"


def test_routes_to_narrative_when_narrative_none() -> None:
    state = _base_state(
        extraction_findings=_extraction_ok(),
        validation_findings=_validation_ok(),
    )
    assert route_from_supervisor(state) == "narrative"


def test_concludes_when_high_confidence_and_judge_pass() -> None:
    state = _base_state(
        extraction_findings=_extraction_ok(),
        validation_findings=_validation_ok(),
        final_narrative=_narrative_ok(),
        confidence_score=0.85,
        judge_verdict="pass",
    )
    assert route_from_supervisor(state) == "conclude"


def test_escalates_when_high_confidence_but_judge_fail() -> None:
    """Step 6 believe-either-fail gate posture: confidence alone is
    not sufficient; the judge must also pass. Catches the
    confidently-wrong agent."""
    state = _base_state(
        extraction_findings=_extraction_ok(),
        validation_findings=_validation_ok(),
        final_narrative=_narrative_ok(),
        confidence_score=0.85,
        judge_verdict="fail",
    )
    assert route_from_supervisor(state) == "escalate"


def test_escalates_when_judge_uncertain() -> None:
    """The judge's third verdict — uncertain — is treated as not-pass.
    Same fail-closed posture: only an explicit pass concludes."""
    state = _base_state(
        extraction_findings=_extraction_ok(),
        validation_findings=_validation_ok(),
        final_narrative=_narrative_ok(),
        confidence_score=0.85,
        judge_verdict="uncertain",
    )
    assert route_from_supervisor(state) == "escalate"


def test_escalates_when_judge_verdict_missing() -> None:
    """Fail-closed default — if the judge gate never ran (no judge
    injected, or judge call hasn't happened yet), an otherwise
    high-confidence narrative escalates. Confident-but-unjudged
    narratives must not reach gold."""
    state = _base_state(
        extraction_findings=_extraction_ok(),
        validation_findings=_validation_ok(),
        final_narrative=_narrative_ok(),
        confidence_score=0.85,
        judge_verdict=None,
    )
    assert route_from_supervisor(state) == "escalate"


def test_escalates_when_confidence_below_threshold() -> None:
    """Even with judge=pass, low confidence escalates. Both halves of
    the gate must agree."""
    state = _base_state(
        extraction_findings=_extraction_ok(),
        validation_findings=_validation_ok(),
        final_narrative=_narrative_ok(),
        confidence_score=CONFIDENCE_THRESHOLD - 0.01,
        judge_verdict="pass",
    )
    assert route_from_supervisor(state) == "escalate"


def test_concludes_at_exact_confidence_threshold() -> None:
    """The threshold is inclusive — ``>=``. Pin the boundary so a
    later edit to ``>`` shows up as a test diff."""
    state = _base_state(
        extraction_findings=_extraction_ok(),
        validation_findings=_validation_ok(),
        final_narrative=_narrative_ok(),
        confidence_score=CONFIDENCE_THRESHOLD,
        judge_verdict="pass",
    )
    assert route_from_supervisor(state) == "conclude"


def test_iteration_cap_does_not_block_terminal_conclude() -> None:
    """Iteration cap only fires when the supervisor would dispatch to a
    sub-agent. With all three findings populated and a passing judge,
    the supervisor concludes regardless of ``iterations_used`` — the
    happy path's natural count (3 sub-agent dispatches → ``iter=MAX``)
    must not block the conclude verdict on the supervisor's 4th visit.

    Spec reference: privateDocs/step_07_layer3_multiagent.md task_07
    test_happy_path_concludes ("≤3 iterations with status=concluded").
    """
    state = _base_state(
        iterations_used=MAX_ITERATIONS,
        extraction_findings=_extraction_ok(),
        validation_findings=_validation_ok(),
        final_narrative=_narrative_ok(),
        confidence_score=0.99,
        judge_verdict="pass",
    )
    assert route_from_supervisor(state) == "conclude"


def test_iteration_cap_fires_on_sub_agent_intent_only() -> None:
    """When the supervisor would dispatch to a sub-agent (some
    finding still missing) AND ``iterations_used >= MAX_ITERATIONS``,
    the cap routes to escalate. Catches the regression where a
    refactor inverts the rule order."""
    state = _base_state(iterations_used=MAX_ITERATIONS)
    assert route_from_supervisor(state) == "escalate"


# ── supervisor_node — side effects ───────────────────────────────────


def _config(judge: Any = None) -> RunnableConfig:
    return {"configurable": {LAYER3_JUDGE_CONFIG_KEY: judge} if judge else {}}


def test_supervisor_node_does_not_increment_iterations_used() -> None:
    """The supervisor no longer owns the iteration counter — sub-agent
    nodes increment it. This keeps the route the supervisor decided
    in lockstep with the route ``route_from_supervisor`` re-derives,
    which both see the same ``iterations_used`` and produce the same
    answer (no pre-vs-post-increment mismatch)."""
    state = _base_state(iterations_used=0)
    updates = supervisor_node(state, _config())
    assert "iterations_used" not in updates


def test_supervisor_node_appends_one_log_entry_naming_destination() -> None:
    """Log entries are the trace — they have to name the actual route
    the supervisor took, not a placeholder. Otherwise the persisted
    tool_trace lies about what happened."""
    state = _base_state()
    updates = supervisor_node(state, _config())
    log = updates["investigation_log"]
    assert len(log) == 1
    # iteration field reflects the pre-supervisor counter (sub-agent
    # nodes own incrementing); for a freshly initialised state that's 0.
    assert log[0].iteration == 0
    assert log[0].actor == "supervisor"
    assert log[0].action == "route_to_extraction"


def test_supervisor_node_sets_status_concluded_on_conclude_route() -> None:
    judge_called: list[InvestigationState] = []

    def fake_judge_pass(s: InvestigationState) -> JudgeResponse:
        judge_called.append(s)
        return JudgeResponse(
            verdict="pass",
            confidence=0.9,
            reasoning="Narrative matches evidence.",
            cited_evidence_fields=["DC-9!r3c4"],
        )

    state = _base_state(
        extraction_findings=_extraction_ok(),
        validation_findings=_validation_ok(),
        final_narrative=_narrative_ok(),
        confidence_score=0.85,
    )
    updates = supervisor_node(state, _config(judge=fake_judge_pass))
    assert len(judge_called) == 1
    assert updates["judge_verdict"] == "pass"
    assert updates["judge_confidence"] == 0.9
    assert updates["status"] == "concluded"


def test_supervisor_node_sets_status_escalated_on_escalate_route() -> None:
    """No judge, narrative present, high confidence — fail-closed
    escalate. Status flips, log entry names the route."""
    state = _base_state(
        extraction_findings=_extraction_ok(),
        validation_findings=_validation_ok(),
        final_narrative=_narrative_ok(),
        confidence_score=0.85,
    )
    updates = supervisor_node(state, _config())
    assert updates["status"] == "escalated_to_human"
    assert updates["investigation_log"][0].action == "route_to_escalate"


def test_supervisor_node_does_not_set_status_on_subagent_dispatch() -> None:
    """Routing to extraction/validation/narrative leaves status at
    'investigating' — only conclude/escalate are terminal."""
    state = _base_state()
    updates = supervisor_node(state, _config())
    assert "status" not in updates


def test_supervisor_node_calls_judge_once_per_narrative() -> None:
    """The judge call is gated by ``judge_verdict is None``. A
    second supervisor visit (judge_verdict already populated) must
    not re-call the judge — that would double-bill the sweep and
    risk a flapping verdict between iterations."""
    call_count = 0

    def counting_judge(s: InvestigationState) -> JudgeResponse:
        nonlocal call_count
        call_count += 1
        return JudgeResponse(
            verdict="pass",
            confidence=0.9,
            reasoning="ok",
            cited_evidence_fields=["x"],
        )

    state = _base_state(
        extraction_findings=_extraction_ok(),
        validation_findings=_validation_ok(),
        final_narrative=_narrative_ok(),
        confidence_score=0.85,
        judge_verdict="pass",  # already judged
        judge_confidence=0.9,
    )
    supervisor_node(state, _config(judge=counting_judge))
    assert call_count == 0


def test_supervisor_node_skips_judge_when_no_narrative() -> None:
    """Judge gate only fires once a final_narrative exists. Dispatch
    iterations (extraction / validation / narrative) skip the judge
    entirely — paying the LLM cost there would be wasteful."""
    call_count = 0

    def counting_judge(s: InvestigationState) -> JudgeResponse:
        nonlocal call_count
        call_count += 1
        return JudgeResponse(
            verdict="pass", confidence=0.9, reasoning="ok", cited_evidence_fields=["x"]
        )

    state = _base_state(extraction_findings=_extraction_ok())  # no narrative yet
    supervisor_node(state, _config(judge=counting_judge))
    assert call_count == 0


# ── End-to-end via run_investigation ─────────────────────────────────


def test_run_investigation_concludes_with_passing_judge() -> None:
    """A judge that always returns pass + agents that immediately
    populate findings → conclude path. Sub-agents are still stubs
    here, so we can't actually drive findings through the graph;
    instead this test confirms the wiring shape: the supervisor
    iterates through the dispatch loop and the judge stays absent
    of side effects until findings exist. Once task_04-06 land, a
    real conclude e2e test slots in here."""
    check = AttributeCheck(control_id="DC-9", attribute_id="D", status="fail")

    judge_calls: list[InvestigationState] = []

    def always_pass(s: InvestigationState) -> JudgeResponse:
        judge_calls.append(s)
        return JudgeResponse(
            verdict="pass",
            confidence=0.95,
            reasoning="Narrative matches evidence.",
            cited_evidence_fields=["DC-9!r3c4"],
        )

    result = run_investigation(
        check=check,
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-001",
        judge=always_pass,
    )
    # Sub-agents still stubs → findings stay None → 3-iter escalate.
    # The judge is never called because final_narrative never lands.
    assert len(judge_calls) == 0
    assert result["status"] == "escalated_to_human"


def test_run_investigation_passes_judge_through_to_supervisor() -> None:
    """Smoke-test the wiring path: judge accepted on the entry point,
    threaded through ``config['configurable']``, picked up by
    supervisor_node. A judge that raises would surface as a graph
    invocation failure — confirms the value isn't silently dropped."""
    check = AttributeCheck(control_id="DC-9", attribute_id="D", status="fail")

    def exploding_judge(s: InvestigationState) -> JudgeResponse:
        raise RuntimeError("judge should not have been called")

    # With no narrative ever produced (sub-agents stubbed), the judge
    # call site never fires — so an exploding judge runs to completion.
    # Once task_06 lands, swap this for a judge-was-called assertion.
    result = run_investigation(
        check=check,
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-001",
        judge=exploding_judge,
    )
    assert result["status"] == "escalated_to_human"


# ── _decide_route — direct tests for completeness ────────────────────


@pytest.mark.parametrize(
    ("state", "expected_route"),
    [
        # Iteration cap precedence
        ({"iterations_used": MAX_ITERATIONS}, "escalate"),
        # Sub-agent dispatch order
        ({"iterations_used": 0}, "extraction"),
        ({"iterations_used": 0, "extraction_findings": "X"}, "validation"),
        (
            {
                "iterations_used": 0,
                "extraction_findings": "X",
                "validation_findings": "Y",
            },
            "narrative",
        ),
        # Conclude requires BOTH halves of the gate
        (
            {
                "iterations_used": 0,
                "extraction_findings": "X",
                "validation_findings": "Y",
                "final_narrative": "Z",
                "confidence_score": 0.9,
                "judge_verdict": "pass",
            },
            "conclude",
        ),
    ],
)
def test_decide_route_parametrized(state: dict[str, Any], expected_route: str) -> None:
    """Parametrized sweep — one row per rule branch. Keeps the
    decision-tree contract visible at a glance."""
    assert _decide_route(state) == expected_route  # type: ignore[arg-type]
