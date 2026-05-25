"""Unit tests for Step 7 task_08 cost-telemetry + decision-builder
wiring inside ``run_investigation``.

Three contracts pinned here:

1. ``UsageRecorder`` threads through all three sub-agents — every
   billed LLM call (Validation, Narrative, Extraction-via-callback)
   records onto the shared recorder. The cost-telemetry row at the
   end carries the aggregate.
2. ``build_layer3_decision_row`` maps the in-process state's joint
   (status, recommendation) signal to the schema's ``final_verdict``
   enum correctly. The mapping is the load-bearing translation
   between the supervisor's terminal state model and the gold-table
   contract.
3. ``run_investigation`` invokes both writers when supplied + skips
   them silently when not. Cost row computed from the recorder
   snapshot; decision row built from the post-``_ensure_terminal_narrative``
   final state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentic_audit.layer3_agents.narrative_agent import NarrativeAgent
from agentic_audit.layer3_agents.state import (
    ExceptionNarrative,
    ExtractionFindings,
    InvestigationState,
    InvestigationStep,
    ValidationFindings,
)
from agentic_audit.layer3_agents.supervisor import (
    _build_cost_telemetry,
    _build_prompt_version_composite,
    build_layer3_decision_row,
    run_investigation,
)
from agentic_audit.layer3_agents.validation_agent import ValidationAgent
from agentic_audit.models.evidence import (
    ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)
from agentic_audit.models.judge import JudgeResponse
from agentic_audit.models.layer3_decision import Layer3Decision
from agentic_audit.models.telemetry import CallUsage, UsageRecorder

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


def _terminal_state(
    *,
    status: str,
    recommendation: str,
    confidence: float = 0.92,
) -> InvestigationState:
    """Build a synthetic terminal InvestigationState for builder tests."""
    return {
        "investigation_run_id": "inv-test",
        "agent_run_id": "sweep-1",
        "engagement_id": "eng-1",
        "control_id": "DC-9",
        "attribute_id": "D",
        "quarter": "Q3",
        "exception_type": "billing_rate_change",
        "current_quarter_evidence": _evidence("DC-9", "Q3"),
        "prior_quarter_evidence": _evidence("DC-9", "Q2"),
        "investigation_log": [
            InvestigationStep(
                iteration=1,
                actor="supervisor",
                action="route_to_extraction",
                timestamp=UTC_TS,
            ),
        ],
        "extraction_findings": ExtractionFindings(
            confidence=0.9, ima_amendment_found=True, old_rate=28.5, new_rate=30.0
        ),
        "validation_findings": ValidationFindings(
            is_authorized=True, confidence=confidence, reasoning="ok"
        ),
        "final_narrative": ExceptionNarrative(
            narrative_text="grounded narrative",
            citations=["sheet1!A12"],
            recommendation=recommendation,  # type: ignore[arg-type]
            word_count=2,
        ),
        "judge_verdict": "pass",
        "judge_confidence": 0.91,
        "confidence_score": confidence,
        "iterations_used": 3,
        "status": status,  # type: ignore[typeddict-item]
    }


# ── _build_cost_telemetry ────────────────────────────────────────────


def test_build_cost_telemetry_aggregates_recorder_snapshot() -> None:
    recorder = UsageRecorder()
    recorder.record(CallUsage(prompt_tokens=100, completion_tokens=50))
    recorder.record(CallUsage(prompt_tokens=200, completion_tokens=80))

    started = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    completed = datetime(2026, 5, 13, 12, 0, 5, tzinfo=UTC)

    telemetry = _build_cost_telemetry(
        agent_run_id="sweep-1",
        recorder=recorder,
        model_deployment="gpt-4o",
        started_at=started,
        completed_at=completed,
    )

    assert telemetry.agent_run_id == "sweep-1"
    assert telemetry.input_tokens == 300
    assert telemetry.output_tokens == 130
    assert telemetry.total_tokens == 430
    assert telemetry.latency_ms == 5000
    assert telemetry.model_version == "gpt-4o"
    # gpt-4o pricing: 0.0025/1k input + 0.0100/1k output → 300*.0025 + 130*.0100 → 0.00075 + 0.0013 = 0.00205
    assert telemetry.cost_usd is not None
    assert telemetry.cost_usd == pytest.approx(0.00205)


def test_build_cost_telemetry_unknown_deployment_yields_null_cost() -> None:
    """Unknown deployment → cost_usd=None per the
    MODEL_PRICING_USD_PER_1K contract; token + latency still persist."""
    recorder = UsageRecorder()
    recorder.record(CallUsage(prompt_tokens=10, completion_tokens=5))

    telemetry = _build_cost_telemetry(
        agent_run_id="sweep-x",
        recorder=recorder,
        model_deployment="gpt-5-secret",  # not in pricing table
        started_at=UTC_TS,
        completed_at=UTC_TS,
    )

    assert telemetry.cost_usd is None
    assert telemetry.input_tokens == 10
    assert telemetry.output_tokens == 5


def test_build_cost_telemetry_handles_empty_recorder() -> None:
    """Fast-path-only investigations (no LLM calls) still produce a
    valid telemetry row — zero tokens, zero cost. Useful for the
    no-document fast-path validation case."""
    recorder = UsageRecorder()
    telemetry = _build_cost_telemetry(
        agent_run_id="sweep-fast",
        recorder=recorder,
        model_deployment="gpt-4o",
        started_at=UTC_TS,
        completed_at=UTC_TS,
    )
    assert telemetry.total_tokens == 0
    assert telemetry.cost_usd == pytest.approx(0.0)


# ── _build_prompt_version_composite ──────────────────────────────────


def test_prompt_version_composite_with_all_three_agents() -> None:
    extraction = ValidationAgent(endpoint="https://fake", client=MagicMock(), prompt_version="v1.0")
    extraction._prompt_version = "v1.0"  # type: ignore[attr-defined]

    # Just check the composite shape — easier to use real instances
    from agentic_audit.layer3_agents.extraction_agent import ExtractionAgent

    ex = ExtractionAgent(endpoint="https://fake", prompt_version="v1.0")
    val = ValidationAgent(endpoint="https://fake", client=MagicMock(), prompt_version="v1.0")
    nar = NarrativeAgent(endpoint="https://fake", client=MagicMock(), prompt_version="v1.1")

    composite = _build_prompt_version_composite(
        extraction_agent=ex, validation_agent=val, narrative_agent=nar
    )
    assert composite == "extraction_v1.0|validation_v1.0|narrative_v1.1"


def test_prompt_version_composite_fills_none_for_missing_agents() -> None:
    composite = _build_prompt_version_composite(
        extraction_agent=None, validation_agent=None, narrative_agent=None
    )
    assert composite == "extraction_none|validation_none|narrative_none"


# ── build_layer3_decision_row ────────────────────────────────────────


def test_decision_row_concluded_accept_maps_to_pass_verdict() -> None:
    state = _terminal_state(status="concluded", recommendation="ACCEPT")
    decision = build_layer3_decision_row(
        state=state,
        prompt_version="extraction_v1.0|validation_v1.0|narrative_v1.1",
        model_deployment="gpt-4o",
        decided_at=UTC_TS,
    )
    assert isinstance(decision, Layer3Decision)
    assert decision.final_verdict == "pass"
    assert decision.status == "concluded"
    assert decision.recommendation == "ACCEPT"


def test_decision_row_concluded_escalate_maps_to_fail_verdict() -> None:
    """Concluded + ESCALATE means automation finished and recommends
    human action — the schema's 'fail' verdict captures "automation
    says this exception is NOT authorised"."""
    state = _terminal_state(status="concluded", recommendation="ESCALATE")
    decision = build_layer3_decision_row(
        state=state,
        prompt_version="x",
        model_deployment="gpt-4o",
        decided_at=UTC_TS,
    )
    assert decision.final_verdict == "fail"


def test_decision_row_escalated_maps_to_uncertain_verdict() -> None:
    """Escalated_to_human → automation couldn't decide → uncertain."""
    state = _terminal_state(status="escalated_to_human", recommendation="ESCALATE")
    decision = build_layer3_decision_row(
        state=state,
        prompt_version="x",
        model_deployment="gpt-4o",
        decided_at=UTC_TS,
    )
    assert decision.final_verdict == "uncertain"


def test_decision_row_serialises_investigation_log_to_json() -> None:
    """tool_trace is the audit artefact — the investigation_log
    serialised as JSON. A reviewer reads it to follow the supervisor's
    routing chain."""
    state = _terminal_state(status="concluded", recommendation="ACCEPT")
    decision = build_layer3_decision_row(
        state=state,
        prompt_version="x",
        model_deployment="gpt-4o",
        decided_at=UTC_TS,
    )
    parsed = json.loads(decision.tool_trace)
    assert isinstance(parsed, list)
    assert parsed[0]["actor"] == "supervisor"
    assert parsed[0]["action"] == "route_to_extraction"


def test_decision_row_raises_on_missing_final_narrative() -> None:
    """Pre-condition: _ensure_terminal_narrative has run, so
    final_narrative is populated. A missing one is a routing bug —
    surface loudly."""
    state = _terminal_state(status="concluded", recommendation="ACCEPT")
    state["final_narrative"] = None
    with pytest.raises(ValueError, match="final_narrative"):
        build_layer3_decision_row(
            state=state, prompt_version="x", model_deployment="gpt-4o", decided_at=UTC_TS
        )


def test_decision_row_raises_on_non_terminal_status() -> None:
    state = _terminal_state(status="concluded", recommendation="ACCEPT")
    state["status"] = "investigating"
    with pytest.raises(ValueError, match="terminal"):
        build_layer3_decision_row(
            state=state, prompt_version="x", model_deployment="gpt-4o", decided_at=UTC_TS
        )


# ── UsageRecorder threading via run_investigation ────────────────────


class _FakeJudgeCallable:
    def __call__(self, state: InvestigationState) -> JudgeResponse:
        return JudgeResponse(
            verdict="pass",
            confidence=0.9,
            reasoning="ok",
            cited_evidence_fields=["x"],
        )


def _validation_agent_with_recorded_call() -> ValidationAgent:
    """ValidationAgent stub whose chat.completions.create returns a
    well-formed payload with usage metadata. The recorder injection
    happens via run_investigation."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(
        {"is_authorized": True, "confidence": 0.92, "reasoning": "ok"}
    )
    response.choices[0].finish_reason = "stop"
    response.usage = MagicMock(prompt_tokens=120, completion_tokens=40)
    client = MagicMock()
    client.chat.completions.create.return_value = response
    return ValidationAgent(endpoint="https://fake", client=client)


def _narrative_agent_with_recorded_call() -> NarrativeAgent:
    """Narrative agent stub. Text is phrased to avoid the
    sentence-initial-bare-capitalised-noun fact-check trip ("Rate"
    isn't in stopwords; "The rate" is) and uses only tokens the
    Layer-3 substrate covers (28.5 / 30.0 from extraction; sheet1!A12
    from anchors)."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(
        {
            "narrative_text": "The rate moved from 28.5 to 30.0.",
            "citations": ["sheet1!A12"],
            "recommendation": "ACCEPT",
            "word_count": 8,
        }
    )
    response.choices[0].finish_reason = "stop"
    response.usage = MagicMock(prompt_tokens=300, completion_tokens=80)
    client = MagicMock()
    client.chat.completions.create.return_value = response
    return NarrativeAgent(endpoint="https://fake", client=client)


class _FakeExtractionAgent:
    """Minimal stand-in. ExtractionAgent's recorder threading goes via
    a langchain callback; testing that needs a different fixture (see
    test_extraction_recorder_callback below). For the run_investigation
    integration test, this fake bumps the recorder directly so the
    aggregate cost row can be asserted."""

    def __init__(self, recorder_to_bump: UsageRecorder | None = None) -> None:
        self.prompt_version = "v1.0"
        self._usage_recorder: UsageRecorder | None = None
        self._explicit_bump = recorder_to_bump

    def invoke(self, state: InvestigationState) -> ExtractionFindings:
        # Bump the recorder run_investigation injected (or the
        # explicit one for solo tests). 50 + 20 tokens for the
        # extraction call.
        target = self._usage_recorder or self._explicit_bump
        if target is not None:
            target.record(CallUsage(prompt_tokens=50, completion_tokens=20))
        return ExtractionFindings(
            confidence=0.9,
            ima_amendment_found=True,
            old_rate=28.5,
            new_rate=30.0,
            evidence_anchors=["sheet1!A12"],
        )


def test_run_investigation_threads_recorder_through_all_three_agents() -> None:
    """All three sub-agents share one recorder and the aggregate
    matches their per-call contributions: 50+20 (extraction) +
    120+40 (validation) + 300+80 (narrative) = 470+140."""
    extraction = _FakeExtractionAgent()
    validation = _validation_agent_with_recorded_call()
    narrative = _narrative_agent_with_recorded_call()

    cost_writer = MagicMock()

    result = run_investigation(
        check=AttributeCheck(control_id="DC-9", attribute_id="D", status="fail"),
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-recorder",
        extraction_agent=extraction,  # type: ignore[arg-type]
        validation_agent=validation,
        narrative_agent=narrative,
        judge=_FakeJudgeCallable(),
        cost_writer=cost_writer,
    )

    assert result["status"] == "concluded"
    cost_writer.write_cost_telemetry.assert_called_once()
    telemetry = cost_writer.write_cost_telemetry.call_args[0][0]
    assert telemetry.agent_run_id == "sweep-recorder"
    assert telemetry.input_tokens == 50 + 120 + 300
    assert telemetry.output_tokens == 20 + 40 + 80
    assert telemetry.total_tokens == 610


def test_run_investigation_skips_writers_when_not_supplied() -> None:
    """Default: no writers wired → no warehouse calls. Recorder still
    accumulates internally but the cost row is silently dropped."""
    extraction = _FakeExtractionAgent()
    validation = _validation_agent_with_recorded_call()
    narrative = _narrative_agent_with_recorded_call()

    result = run_investigation(
        check=AttributeCheck(control_id="DC-9", attribute_id="D", status="fail"),
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-no-writer",
        extraction_agent=extraction,  # type: ignore[arg-type]
        validation_agent=validation,
        narrative_agent=narrative,
        judge=_FakeJudgeCallable(),
    )

    assert result["status"] == "concluded"


def test_run_investigation_invokes_decisions_writer_with_built_row() -> None:
    extraction = _FakeExtractionAgent()
    validation = _validation_agent_with_recorded_call()
    narrative = _narrative_agent_with_recorded_call()
    decisions_writer = MagicMock()

    run_investigation(
        check=AttributeCheck(control_id="DC-9", attribute_id="D", status="fail"),
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-decisions",
        extraction_agent=extraction,  # type: ignore[arg-type]
        validation_agent=validation,
        narrative_agent=narrative,
        judge=_FakeJudgeCallable(),
        decisions_writer=decisions_writer,
    )

    decisions_writer.write_decision.assert_called_once()
    decision = decisions_writer.write_decision.call_args[0][0]
    assert isinstance(decision, Layer3Decision)
    assert decision.agent_run_id == "sweep-decisions"
    assert decision.final_verdict == "pass"  # concluded + ACCEPT
    assert decision.prompt_version.startswith("extraction_v1.0|validation_v1.0|narrative_v1.1")


def test_run_investigation_writes_decision_row_on_iteration_cap_escalate() -> None:
    """Even the early-cap escalate path writes a complete row — the
    degraded escalation narrative + uncertain verdict combine cleanly."""
    decisions_writer = MagicMock()

    run_investigation(
        check=AttributeCheck(control_id="DC-9", attribute_id="D", status="fail"),
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-cap-escalate",
        decisions_writer=decisions_writer,
    )

    decisions_writer.write_decision.assert_called_once()
    decision = decisions_writer.write_decision.call_args[0][0]
    assert decision.status == "escalated_to_human"
    assert decision.final_verdict == "uncertain"
    assert decision.recommendation == "ESCALATE"
    assert decision.citations == []  # degraded narrative empty citations
    assert "human review required" in decision.narrative_text


# ── Validation + Narrative agent recorder-injection unit tests ───────


def test_validation_agent_records_usage_when_recorder_wired() -> None:
    """Direct unit test for the per-agent recording — independent of
    run_investigation's injection. ValidationAgent.invoke records the
    chat.completions.create call's usage when a recorder is wired."""
    recorder = UsageRecorder()
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(
        {"is_authorized": True, "confidence": 0.9, "reasoning": "ok"}
    )
    response.choices[0].finish_reason = "stop"
    response.usage = MagicMock(prompt_tokens=42, completion_tokens=17)
    client = MagicMock()
    client.chat.completions.create.return_value = response
    agent = ValidationAgent(endpoint="https://fake", client=client, usage_recorder=recorder)

    state: InvestigationState = {  # type: ignore[typeddict-item]
        "engagement_id": "e1",
        "control_id": "DC-9",
        "attribute_id": "D",
        "quarter": "Q3",
        "exception_type": "billing_rate_change",
        "extraction_findings": ExtractionFindings(
            confidence=0.9,
            ima_amendment_found=True,
            old_rate=28.5,
            new_rate=30.0,
            ima_amendment_text="text",
        ),
    }
    agent.invoke(state)

    assert recorder.n_calls == 1
    assert recorder.prompt_tokens == 42
    assert recorder.completion_tokens == 17


def test_validation_agent_skips_recording_on_fast_path() -> None:
    """No-document fast path returns without an LLM call → the
    recorder must stay at zero."""
    recorder = UsageRecorder()
    agent = ValidationAgent(endpoint="https://fake", client=MagicMock(), usage_recorder=recorder)

    state: InvestigationState = {  # type: ignore[typeddict-item]
        "exception_type": "billing_rate_change",
        "extraction_findings": ExtractionFindings(
            confidence=0.9,
            ima_amendment_found=False,  # triggers fast path
        ),
        "engagement_id": "e1",
        "control_id": "DC-9",
        "attribute_id": "D",
        "quarter": "Q3",
    }
    agent.invoke(state)

    assert recorder.n_calls == 0
    assert recorder.prompt_tokens == 0


def test_narrative_agent_records_usage_when_recorder_wired() -> None:
    recorder = UsageRecorder()
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(
        {
            "narrative_text": "The rate moved from 28.5 to 30.0.",
            "citations": ["sheet1!A12"],
            "recommendation": "ACCEPT",
            "word_count": 8,
        }
    )
    response.choices[0].finish_reason = "stop"
    response.usage = MagicMock(prompt_tokens=88, completion_tokens=22)
    client = MagicMock()
    client.chat.completions.create.return_value = response
    agent = NarrativeAgent(endpoint="https://fake", client=client, usage_recorder=recorder)

    extraction = ExtractionFindings(
        confidence=0.9,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_text="text",
        evidence_anchors=["sheet1!A12"],
    )
    validation = ValidationFindings(is_authorized=True, confidence=0.9, reasoning="ok")
    state: InvestigationState = {  # type: ignore[typeddict-item]
        "engagement_id": "eng-1",
        "control_id": "DC-9",
        "attribute_id": "D",
        "quarter": "Q3",
        "exception_type": "billing_rate_change",
        "current_quarter_evidence": _evidence("DC-9", "Q3"),
        "prior_quarter_evidence": _evidence("DC-9", "Q2"),
        "extraction_findings": extraction,
        "validation_findings": validation,
    }
    agent.invoke(state)

    assert recorder.n_calls == 1
    assert recorder.prompt_tokens == 88
    assert recorder.completion_tokens == 22


# ── ExtractionAgent callback handler ─────────────────────────────────


def _inmemory_tracer() -> tuple[Any, Any]:
    """Build an OTel tracer routed to an in-memory exporter for span
    assertions. Returns ``(tracer, exporter)``. SimpleSpanProcessor
    exports on ``span.end()`` so finished spans are visible
    synchronously."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def _llm_result(prompt_tokens: int, completion_tokens: int) -> Any:
    """Minimal LangChain ``LLMResult``-shaped object carrying usage."""
    result = MagicMock()
    result.llm_output = {
        "token_usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
        "model_name": "gpt-4o",
    }
    return result


def test_extraction_callback_records_usage_from_llm_result() -> None:
    """ExtractionAgent's _TelemetryCallbackHandler reads
    ``LLMResult.llm_output['token_usage']`` and pushes it onto the
    recorder. Pin the contract directly without booting the full
    ReAct loop."""
    from agentic_audit.layer3_agents.extraction_agent import (
        _TelemetryCallbackHandler,
    )

    recorder = UsageRecorder()
    handler = _TelemetryCallbackHandler(recorder)

    handler.on_llm_end(_llm_result(70, 30))

    assert recorder.n_calls == 1
    assert recorder.prompt_tokens == 70
    assert recorder.completion_tokens == 30


def test_extraction_callback_silent_on_missing_token_usage() -> None:
    """Defensive: some LLMResult shapes don't carry token_usage (e.g.
    streaming-mode partials). The handler must silently no-op rather
    than raise into the agent loop."""
    from agentic_audit.layer3_agents.extraction_agent import (
        _TelemetryCallbackHandler,
    )

    recorder = UsageRecorder()
    handler = _TelemetryCallbackHandler(recorder)

    fake_result = MagicMock()
    fake_result.llm_output = None
    handler.on_llm_end(fake_result)

    assert recorder.n_calls == 0


def test_extraction_callback_emits_llm_span_per_call(monkeypatch: Any) -> None:
    """A start→end pair emits one ``llm.layer3_extraction`` span with the
    same ``llm.*`` attribute schema the kit decorator produces for
    narrative + validation — what Workbook 2 (Cost & Tokens) reads."""
    import uuid

    from agentic_audit.layer3_agents import extraction_agent
    from agentic_audit.layer3_agents.extraction_agent import (
        _TelemetryCallbackHandler,
    )

    tracer, exporter = _inmemory_tracer()
    monkeypatch.setattr(extraction_agent, "_TRACER", tracer)

    handler = _TelemetryCallbackHandler(UsageRecorder(), deployment="gpt-4o")
    run_id = uuid.uuid4()
    handler.on_chat_model_start({}, [], run_id=run_id)
    handler.on_llm_end(_llm_result(70, 30), run_id=run_id)

    spans = [s for s in exporter.get_finished_spans() if s.name == "llm.layer3_extraction"]
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["llm.prompt_tokens"] == 70
    assert attrs["llm.completion_tokens"] == 30
    assert attrs["llm.total_tokens"] == 100
    assert attrs["llm.model_version"] == "gpt-4o"
    assert attrs["llm.success"] is True
    assert attrs["llm.total_cost_usd"] > 0  # gpt-4o is in the pricing table
    assert "llm.duration_ms" in attrs


def test_extraction_callback_emits_one_span_per_loop_call(monkeypatch: Any) -> None:
    """The whole point of per-call instrumentation: a 2-call ReAct loop
    emits 2 distinct spans (not 1 aggregate), each keyed by its own
    LangChain run_id so durations + tokens stay per-call."""
    import uuid

    from agentic_audit.layer3_agents import extraction_agent
    from agentic_audit.layer3_agents.extraction_agent import (
        _TelemetryCallbackHandler,
    )

    tracer, exporter = _inmemory_tracer()
    monkeypatch.setattr(extraction_agent, "_TRACER", tracer)

    handler = _TelemetryCallbackHandler(UsageRecorder(), deployment="gpt-4o")
    rid1, rid2 = uuid.uuid4(), uuid.uuid4()
    handler.on_chat_model_start({}, [], run_id=rid1)
    handler.on_llm_end(_llm_result(320, 80), run_id=rid1)
    handler.on_chat_model_start({}, [], run_id=rid2)
    handler.on_llm_end(_llm_result(540, 120), run_id=rid2)

    spans = [s for s in exporter.get_finished_spans() if s.name == "llm.layer3_extraction"]
    assert len(spans) == 2
    totals = sorted(s.attributes["llm.total_tokens"] for s in spans)
    assert totals == [400, 660]


def test_extraction_callback_span_marks_error_and_closes(monkeypatch: Any) -> None:
    """A failed call closes its span (no leak) with ``llm.success=False``
    and ERROR status — mirrors the kit decorator's error path."""
    import uuid

    from opentelemetry.trace import StatusCode

    from agentic_audit.layer3_agents import extraction_agent
    from agentic_audit.layer3_agents.extraction_agent import (
        _TelemetryCallbackHandler,
    )

    tracer, exporter = _inmemory_tracer()
    monkeypatch.setattr(extraction_agent, "_TRACER", tracer)

    handler = _TelemetryCallbackHandler(None, deployment="gpt-4o")
    run_id = uuid.uuid4()
    handler.on_chat_model_start({}, [], run_id=run_id)
    handler.on_llm_error(RuntimeError("rate limited"), run_id=run_id)

    spans = [s for s in exporter.get_finished_spans() if s.name == "llm.layer3_extraction"]
    assert len(spans) == 1
    assert spans[0].attributes["llm.success"] is False
    assert spans[0].status.status_code == StatusCode.ERROR


def test_extraction_callback_span_closes_without_usage(monkeypatch: Any) -> None:
    """Missing token_usage still closes the span (no leak) with
    success=True and no token attrs; the recorder is left untouched."""
    import uuid

    from agentic_audit.layer3_agents import extraction_agent
    from agentic_audit.layer3_agents.extraction_agent import (
        _TelemetryCallbackHandler,
    )

    tracer, exporter = _inmemory_tracer()
    monkeypatch.setattr(extraction_agent, "_TRACER", tracer)

    recorder = UsageRecorder()
    handler = _TelemetryCallbackHandler(recorder, deployment="gpt-4o")
    run_id = uuid.uuid4()
    handler.on_chat_model_start({}, [], run_id=run_id)
    no_usage = MagicMock()
    no_usage.llm_output = None
    handler.on_llm_end(no_usage, run_id=run_id)

    spans = [s for s in exporter.get_finished_spans() if s.name == "llm.layer3_extraction"]
    assert len(spans) == 1
    assert spans[0].attributes["llm.success"] is True
    assert "llm.prompt_tokens" not in spans[0].attributes
    assert recorder.n_calls == 0


# ── @traced_function smoke ───────────────────────────────────────────


def test_run_investigation_emits_traced_function_span(caplog: Any) -> None:
    """@traced_function on run_investigation must emit a span_start /
    span_end pair on the agentic_audit.trace logger. Cheap end-to-end
    proof that the OTel-style instrumentation actually fires."""
    import logging

    caplog.set_level(logging.INFO, logger="agentic_audit.trace")

    extraction = _FakeExtractionAgent()
    validation = _validation_agent_with_recorded_call()
    narrative = _narrative_agent_with_recorded_call()

    run_investigation(
        check=AttributeCheck(control_id="DC-9", attribute_id="D", status="fail"),
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-trace",
        extraction_agent=extraction,  # type: ignore[arg-type]
        validation_agent=validation,
        narrative_agent=narrative,
        judge=_FakeJudgeCallable(),
    )

    # @traced_function emits the span name on the log record's
    # ``extra={'span': ...}`` field, not in the message text. Read
    # the structured field directly.
    span_names = [getattr(r, "span", None) for r in caplog.records]
    span_names = [s for s in span_names if s is not None]
    # At minimum, run_investigation's own span fired
    assert any("layer3.run_investigation" in s for s in span_names)
    # Sub-agent spans fired too (proves the @traced_function decorators
    # on the node + agent functions are wired)
    assert any("layer3.extraction_agent" in s for s in span_names)
    assert any("layer3.validation_agent" in s for s in span_names)
    assert any("layer3.narrative_agent" in s for s in span_names)
