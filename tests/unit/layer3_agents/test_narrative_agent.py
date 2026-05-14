"""Unit tests for ``agentic_audit.layer3_agents.narrative_agent`` and
the supervisor's ``narrative_agent_node`` wiring — Step 7 task_06.

Six contracts pinned here:

1. The two prompt templates load and carry the required substitution
   variables + the structured-output schema markers.
2. ``NarrativeAgent`` renders the correct per-exception-type prompt
   with extraction + validation findings substituted in. Wrong prompt =
   wrong narrative shape downstream.
3. The LLM happy path returns a parsed ``ExceptionNarrative`` and,
   when the narrative grounds against the substrate, surfaces it
   directly.
4. The word-limit retry path: first response over 200 words triggers
   a stricter retry; second response under the limit ships; both over
   triggers truncation to 200 words.
5. The fact-check retry path: first response with un-grounded
   numerics / entities triggers a stricter retry. Both attempts
   un-grounded fall back to the deterministic ESCALATE narrative.
6. The supervisor's ``narrative_agent_node`` wires the injected agent
   through ``config["configurable"]["layer3_narrative_agent"]``,
   falls back to no-op when absent, propagates
   ``validation_findings.confidence`` into ``confidence_score``, and
   appends the trace entry on success.

The real LLM call is exercised by the env-gated slow test in
``tests/integration/test_layer3_narrative_agent_e2e.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.runnables import RunnableConfig

from agentic_audit.layer3_agents.narrative_agent import (
    PROMPTS_DIR,
    WORD_LIMIT,
    NarrativeAgent,
    _l3_fact_check,
    _l3_fact_check_substrate,
)
from agentic_audit.layer3_agents.state import (
    ExceptionNarrative,
    ExtractionFindings,
    InvestigationState,
    ValidationFindings,
)
from agentic_audit.layer3_agents.supervisor import (
    LAYER3_NARRATIVE_AGENT_CONFIG_KEY,
    narrative_agent_node,
    run_investigation,
)
from agentic_audit.models.evidence import (
    ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)

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


def _state(
    exception_type: str,
    control_id: str,
    attribute_id: str,
    *,
    extraction: ExtractionFindings | None = None,
    validation: ValidationFindings | None = None,
) -> InvestigationState:
    return {
        "investigation_run_id": "inv-test",
        "agent_run_id": "sweep-1",
        "engagement_id": "eng-1",
        "control_id": control_id,  # type: ignore[typeddict-item]
        "attribute_id": attribute_id,  # type: ignore[typeddict-item]
        "quarter": "Q3",
        "exception_type": exception_type,  # type: ignore[typeddict-item]
        "current_quarter_evidence": _evidence(control_id, "Q3"),
        "prior_quarter_evidence": _evidence(control_id, "Q2"),
        "investigation_log": [],
        "extraction_findings": extraction,
        "validation_findings": validation,
        "final_narrative": None,
        "judge_verdict": None,
        "judge_confidence": None,
        "confidence_score": 0.0,
        "iterations_used": 2,
        "status": "investigating",
    }


def _stub_client_returning_jsons(*payloads: dict[str, Any]) -> MagicMock:
    """Build a MagicMock client whose successive calls return each
    JSON-serialised payload in sequence."""
    responses = []
    for payload in payloads:
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = json.dumps(payload)
        response.choices[0].finish_reason = "stop"
        responses.append(response)
    client = MagicMock()
    client.chat.completions.create.side_effect = responses
    return client


# ── Prompt templates ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename",
    [
        "narrative_v1_1_exception_dc9d.txt",
        "narrative_v1_1_exception_dc2b.txt",
    ],
)
def test_prompt_template_loads_and_carries_required_markers(filename: str) -> None:
    """Every Narrative prompt MUST:

    - exist on disk under the layer3 prompts dir
    - declare the four common substitution variables + the validation
      block + word_limit
    - reference ExceptionNarrative's four output keys
    """
    text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
    for placeholder in (
        "${engagement_id}",
        "${control_id}",
        "${attribute_id}",
        "${quarter}",
        "${validation_is_authorized}",
        "${validation_confidence}",
        "${validation_reasoning}",
        "${evidence_anchors}",
        "${word_limit}",
    ):
        assert placeholder in text, f"{filename} missing placeholder {placeholder}"
    for key in ("narrative_text", "citations", "recommendation", "word_count"):
        assert key in text, f"{filename} missing schema key {key}"


def test_dc9d_prompt_has_billing_subset_placeholders() -> None:
    text = (PROMPTS_DIR / "narrative_v1_1_exception_dc9d.txt").read_text()
    for placeholder in ("${old_rate}", "${new_rate}", "${ima_amendment_text}"):
        assert placeholder in text, f"dc9d narrative prompt missing {placeholder}"


def test_dc2b_prompt_has_variance_subset_placeholders() -> None:
    text = (PROMPTS_DIR / "narrative_v1_1_exception_dc2b.txt").read_text()
    for placeholder in ("${variance_magnitude}", "${variance_explanation_text}"):
        assert placeholder in text, f"dc2b narrative prompt missing {placeholder}"


# ── Layer-3 fact-check substrate ─────────────────────────────────────


def test_l3_substrate_includes_extraction_validation_and_anchors() -> None:
    """The Layer-3 fact-check substrate must let a narrative ground
    against any of: raw evidence, extraction findings, validation
    reasoning, or the cell-ref anchors. A naive Layer-2 substrate
    (raw evidence only) would reject all extraction-derived claims."""
    extraction = ExtractionFindings(
        confidence=0.9,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_text="Effective Q3 2026, the management fee rate shall be 30.0 bps.",
        evidence_anchors=["sheet1!A12", "amendment.pdf!p2"],
    )
    validation = ValidationFindings(
        is_authorized=True,
        confidence=0.92,
        reasoning="The amendment effective Q3 2026 explicitly authorises 30.0 bps.",
    )
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction, validation=validation)

    substrate = _l3_fact_check_substrate(state, extraction, validation)

    # All four sources represented
    assert "28.5" in substrate
    assert "30.0" in substrate
    assert "Effective Q3 2026" in substrate
    assert "sheet1!A12" in substrate
    assert "amendment.pdf!p2" in substrate
    assert "explicitly authorises" in substrate


def test_l3_fact_check_passes_grounded_narrative() -> None:
    extraction = ExtractionFindings(
        confidence=0.9,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_text="Effective Q3 2026, the management fee rate shall be 30.0 bps.",
        evidence_anchors=["sheet1!A12"],
    )
    validation = ValidationFindings(is_authorized=True, confidence=0.92, reasoning="ok")
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction, validation=validation)
    substrate = _l3_fact_check_substrate(state, extraction, validation)

    # Sentence-initial capitalised common nouns ("Rate", "Per", etc.)
    # are NOT in the entity stopword list; phrasing intentionally
    # avoids them (mirrors what production prompts steer the LLM
    # toward — see Layer 2 v1.1 prompt revision rationale).
    text = "The rate moved from 28.5 to 30.0 per the amendment Effective Q3 2026."
    passed, issues = _l3_fact_check(text, substrate)
    assert passed, f"expected grounded narrative to pass, issues={issues}"


def test_l3_fact_check_flags_ungrounded_numeric() -> None:
    extraction = ExtractionFindings(
        confidence=0.9, ima_amendment_found=True, old_rate=28.5, new_rate=30.0
    )
    validation = ValidationFindings(is_authorized=True, confidence=0.9, reasoning="ok")
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction, validation=validation)
    substrate = _l3_fact_check_substrate(state, extraction, validation)

    # Bogus number 99.99 isn't anywhere in the substrate
    text = "Rate hike of 99.99 bps not in the evidence."
    passed, issues = _l3_fact_check(text, substrate)
    assert not passed
    assert any("99.99" in issue for issue in issues)


# ── NarrativeAgent — pre-conditions ──────────────────────────────────


def test_invoke_raises_when_extraction_missing() -> None:
    """Pre-condition: supervisor only routes here after extraction +
    validation land. Empty extraction is a routing bug."""
    agent = NarrativeAgent(endpoint="https://fake", client=MagicMock())
    state = _state(
        "billing_rate_change",
        "DC-9",
        "D",
        extraction=None,
        validation=ValidationFindings(is_authorized=True, confidence=0.9, reasoning="x"),
    )
    with pytest.raises(ValueError, match="extraction_findings"):
        agent.invoke(state)


def test_invoke_raises_when_validation_missing() -> None:
    agent = NarrativeAgent(endpoint="https://fake", client=MagicMock())
    state = _state(
        "billing_rate_change",
        "DC-9",
        "D",
        extraction=ExtractionFindings(confidence=0.9, ima_amendment_found=True),
        validation=None,
    )
    with pytest.raises(ValueError, match="validation_findings"):
        agent.invoke(state)


# ── NarrativeAgent — prompt rendering ────────────────────────────────


def test_renders_dc9d_prompt_with_extraction_and_validation_substituted() -> None:
    extraction = ExtractionFindings(
        confidence=0.92,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_text="Effective Q3 2026, fee rate shall be 30.0 bps.",
        evidence_anchors=["sheet1!A12", "amendment.pdf!p2"],
    )
    validation = ValidationFindings(
        is_authorized=True,
        confidence=0.95,
        reasoning="Amendment explicitly authorises 30.0 bps from Q3.",
    )
    agent = NarrativeAgent(endpoint="https://fake", client=MagicMock())
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction, validation=validation)

    rendered = agent._peek_rendered_prompt(state)

    assert "${engagement_id}" not in rendered
    assert "eng-1" in rendered
    assert "DC-9" in rendered
    assert "Q3" in rendered
    assert "28.5" in rendered
    assert "30.0" in rendered
    assert "Effective Q3 2026" in rendered
    assert "sheet1!A12" in rendered
    assert "amendment.pdf!p2" in rendered
    assert "True" in rendered  # validation_is_authorized
    assert "0.95" in rendered  # validation_confidence
    assert "explicitly authorises" in rendered
    assert "200" in rendered  # word_limit


def test_renders_dc2b_prompt_with_variance_facts_substituted() -> None:
    extraction = ExtractionFindings(
        confidence=0.88,
        variance_explanation_found=True,
        variance_magnitude=0.42,
        variance_explanation_text="Q3 mandate change increased AUM by 38%.",
        evidence_anchors=["sheet2!B7"],
    )
    validation = ValidationFindings(
        is_authorized=True, confidence=0.81, reasoning="Mandate change cited."
    )
    agent = NarrativeAgent(endpoint="https://fake", client=MagicMock())
    state = _state(
        "variance_plausibility", "DC-2", "B", extraction=extraction, validation=validation
    )

    rendered = agent._peek_rendered_prompt(state)

    assert "0.42" in rendered
    assert "mandate change" in rendered
    assert "0.81" in rendered


# ── NarrativeAgent — LLM happy path ──────────────────────────────────


def _grounded_payload() -> dict[str, Any]:
    """A payload that grounds against the standard fixture state —
    every numeric (28.5, 30.0) and entity (sheet1!A12, Effective, Q3)
    is in the substrate. Phrasing avoids sentence-initial bare
    capitalised common nouns (e.g. "Rate") which would trip the
    entity grounder — see ``_l3_fact_check_passes_grounded_narrative``
    test for the discussion."""
    return {
        "narrative_text": ("The rate moved from 28.5 to 30.0 per the amendment Effective Q3 2026."),
        "citations": ["sheet1!A12"],
        "recommendation": "ACCEPT",
        "word_count": 14,
    }


def _fixture_extraction_validation() -> tuple[ExtractionFindings, ValidationFindings]:
    extraction = ExtractionFindings(
        confidence=0.92,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_text="Effective Q3 2026, fee rate shall be 30.0 bps.",
        evidence_anchors=["sheet1!A12"],
    )
    validation = ValidationFindings(is_authorized=True, confidence=0.95, reasoning="ok.")
    return extraction, validation


def test_llm_happy_path_returns_grounded_accept_narrative() -> None:
    extraction, validation = _fixture_extraction_validation()
    client = _stub_client_returning_jsons(_grounded_payload())
    agent = NarrativeAgent(endpoint="https://fake", client=client)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction, validation=validation)

    narrative = agent.invoke(state)

    assert isinstance(narrative, ExceptionNarrative)
    assert narrative.recommendation == "ACCEPT"
    assert "28.5" in narrative.narrative_text
    assert "30.0" in narrative.narrative_text
    client.chat.completions.create.assert_called_once()


# ── NarrativeAgent — word-limit retry ────────────────────────────────


def _build_long_payload(word_count: int) -> dict[str, Any]:
    """Build a grounded payload whose narrative_text has the given
    word count. Words are repeated ground-truth tokens so the
    fact-checker still passes."""
    base_words = ["28.5", "30.0", "amendment", "Q3", "2026"]
    text_words = [base_words[i % len(base_words)] for i in range(word_count)]
    return {
        "narrative_text": " ".join(text_words),
        "citations": ["sheet1!A12"],
        "recommendation": "ACCEPT",
        "word_count": word_count,
    }


def test_word_limit_retry_on_overrun_then_under_limit() -> None:
    """First response over 200 words → stricter retry. Second response
    under the limit → ship the second."""
    extraction, validation = _fixture_extraction_validation()
    over_payload = _build_long_payload(word_count=250)
    under_payload = _build_long_payload(word_count=120)
    client = _stub_client_returning_jsons(over_payload, under_payload)
    agent = NarrativeAgent(endpoint="https://fake", client=client)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction, validation=validation)

    narrative = agent.invoke(state)

    assert narrative.word_count <= WORD_LIMIT
    assert client.chat.completions.create.call_count == 2


def test_word_limit_truncates_when_both_attempts_overrun() -> None:
    """Two consecutive over-limit responses → truncate to WORD_LIMIT.

    The truncated narrative still grounds (built from repeated
    in-evidence tokens), so it passes the post-truncation fact-check
    and ships rather than falling back to ESCALATE."""
    extraction, validation = _fixture_extraction_validation()
    over_1 = _build_long_payload(word_count=250)
    over_2 = _build_long_payload(word_count=240)
    client = _stub_client_returning_jsons(over_1, over_2)
    agent = NarrativeAgent(endpoint="https://fake", client=client)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction, validation=validation)

    narrative = agent.invoke(state)

    assert narrative.word_count == WORD_LIMIT
    assert len(narrative.narrative_text.split()) == WORD_LIMIT


# ── NarrativeAgent — fact-check retry ────────────────────────────────


def test_fact_check_retry_then_grounded() -> None:
    """First response with un-grounded numeric → stricter retry. Second
    response is grounded → ship the second."""
    extraction, validation = _fixture_extraction_validation()
    ungrounded = {
        "narrative_text": "Rate hike of 99.99 not in evidence.",
        "citations": ["sheet1!A12"],
        "recommendation": "ESCALATE",
        "word_count": 7,
    }
    client = _stub_client_returning_jsons(ungrounded, _grounded_payload())
    agent = NarrativeAgent(endpoint="https://fake", client=client)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction, validation=validation)

    narrative = agent.invoke(state)

    assert "28.5" in narrative.narrative_text  # second (grounded) response shipped
    assert client.chat.completions.create.call_count == 2


def test_fact_check_falls_back_to_escalate_after_two_failures() -> None:
    """Two consecutive un-grounded narratives → fallback ESCALATE."""
    extraction, validation = _fixture_extraction_validation()
    ungrounded_1 = {
        "narrative_text": "Rate jump of 99.99 not in payload.",
        "citations": ["sheet1!A12"],
        "recommendation": "ESCALATE",
        "word_count": 7,
    }
    ungrounded_2 = {
        "narrative_text": "Different bogus value 77.77 also not present.",
        "citations": ["sheet1!A12"],
        "recommendation": "ESCALATE",
        "word_count": 7,
    }
    client = _stub_client_returning_jsons(ungrounded_1, ungrounded_2)
    agent = NarrativeAgent(endpoint="https://fake", client=client)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction, validation=validation)

    narrative = agent.invoke(state)

    assert narrative.recommendation == "ESCALATE"
    assert "human review required" in narrative.narrative_text
    assert narrative.citations == []
    assert client.chat.completions.create.call_count == 2


# ── NarrativeAgent — parse failure path ──────────────────────────────


def test_llm_parse_failure_on_both_attempts_falls_back_to_escalate() -> None:
    """Two consecutive malformed-JSON responses → fallback ESCALATE.
    NarrativeAgent must NEVER raise into the supervisor."""
    extraction, validation = _fixture_extraction_validation()
    bad = MagicMock()
    bad.choices = [MagicMock()]
    bad.choices[0].message.content = "not json {"
    bad.choices[0].finish_reason = "stop"
    client = MagicMock()
    client.chat.completions.create.side_effect = [bad, bad]
    agent = NarrativeAgent(endpoint="https://fake", client=client)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction, validation=validation)

    narrative = agent.invoke(state)

    assert narrative.recommendation == "ESCALATE"
    assert "human review required" in narrative.narrative_text


# ── narrative_agent_node — supervisor wiring ─────────────────────────


class _FakeNarrativeAgent:
    """Quack-typed stand-in mirroring the task_05 supervisor-test
    helpers."""

    def __init__(self, narrative: ExceptionNarrative) -> None:
        self._narrative = narrative
        self.calls: list[InvestigationState] = []

    def invoke(self, state: InvestigationState) -> ExceptionNarrative:
        self.calls.append(state)
        return self._narrative


def _config(agent: Any = None) -> RunnableConfig:
    return {
        "configurable": ({LAYER3_NARRATIVE_AGENT_CONFIG_KEY: agent} if agent is not None else {})
    }


def test_narrative_agent_node_runs_injected_agent_and_propagates_confidence() -> None:
    extraction = ExtractionFindings(confidence=0.9, ima_amendment_found=True)
    validation = ValidationFindings(is_authorized=True, confidence=0.88, reasoning="ok")
    narrative = ExceptionNarrative(
        narrative_text="text", citations=["c1"], recommendation="ACCEPT", word_count=1
    )
    fake = _FakeNarrativeAgent(narrative)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction, validation=validation)

    updates = narrative_agent_node(state, _config(agent=fake))

    assert updates["final_narrative"] is narrative
    assert updates["confidence_score"] == pytest.approx(0.88)  # from validation
    assert len(updates["investigation_log"]) == 1
    assert updates["investigation_log"][0].actor == "narrative_agent"


def test_narrative_agent_node_no_op_emits_iter_increment_and_log_entry() -> None:
    """Fail-closed default — no agent injected. The node still emits a
    state delta (iter increment + ``no_agent_injected_no_op`` log
    entry) so the LangGraph super-step engine doesn't declare early
    convergence on a no-state-change return."""
    extraction = ExtractionFindings(confidence=0.9, ima_amendment_found=True)
    validation = ValidationFindings(is_authorized=True, confidence=0.9, reasoning="ok")
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction, validation=validation)
    updates = narrative_agent_node(state, _config())
    assert updates["iterations_used"] == state["iterations_used"] + 1  # type: ignore[operator]
    step = updates["investigation_log"][0]
    assert step.actor == "narrative_agent"
    assert step.action == "no_agent_injected_no_op"
    assert "final_narrative" not in updates


# ── End-to-end via run_investigation ─────────────────────────────────


class _FakeExtractionAgent:
    def __init__(self, findings: ExtractionFindings) -> None:
        self._findings = findings

    def invoke(self, state: InvestigationState) -> ExtractionFindings:
        return self._findings


class _FakeValidationAgent:
    def __init__(self, findings: ValidationFindings) -> None:
        self._findings = findings

    def invoke(self, state: InvestigationState) -> ValidationFindings:
        return self._findings


class _FakeJudge:
    """Returns a pass verdict — feeds the supervisor's conclude gate
    so the e2e test exercises the happy path through to status=concluded."""

    def __init__(self, verdict: str = "pass", confidence: float = 0.9) -> None:
        self._verdict = verdict
        self._confidence = confidence

    def __call__(self, state: InvestigationState) -> Any:
        from agentic_audit.models.judge import JudgeResponse

        return JudgeResponse(
            verdict=self._verdict,  # type: ignore[arg-type]
            confidence=self._confidence,
            reasoning="judge ok",
            cited_evidence_fields=["x"],
        )


def test_run_investigation_threads_narrative_agent_and_concludes_on_happy_path() -> None:
    """All three sub-agents wired + a passing judge → supervisor
    concludes with status='concluded'. Confidence flows from
    validation → confidence_score, judge=pass + confidence>0.7 →
    conclude."""
    extraction = ExtractionFindings(
        confidence=0.9, ima_amendment_found=True, old_rate=28.5, new_rate=30.0
    )
    validation = ValidationFindings(is_authorized=True, confidence=0.92, reasoning="ok")
    narrative = ExceptionNarrative(
        narrative_text="Rate 28.5 -> 30.0 per amendment.",
        citations=["sheet1!A12"],
        recommendation="ACCEPT",
        word_count=6,
    )
    check = AttributeCheck(control_id="DC-9", attribute_id="D", status="fail")

    result = run_investigation(
        check=check,
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-1",
        extraction_agent=_FakeExtractionAgent(extraction),  # type: ignore[arg-type]
        validation_agent=_FakeValidationAgent(validation),  # type: ignore[arg-type]
        narrative_agent=_FakeNarrativeAgent(narrative),  # type: ignore[arg-type]
        judge=_FakeJudge(verdict="pass", confidence=0.9),  # type: ignore[arg-type]
    )

    assert result["status"] == "concluded"
    assert result["final_narrative"] is not None
    assert result["final_narrative"].recommendation == "ACCEPT"  # type: ignore[union-attr]
    assert result["confidence_score"] == pytest.approx(0.92)
    assert result["judge_verdict"] == "pass"


def test_run_investigation_escalates_when_judge_fails_even_with_confident_narrative() -> None:
    """Believe-either-fail gate: narrative + extraction + validation all
    succeed and confidence is high, but judge=fail → escalate. The
    judge gate is the second line of defence."""
    extraction = ExtractionFindings(
        confidence=0.9, ima_amendment_found=True, old_rate=28.5, new_rate=30.0
    )
    validation = ValidationFindings(is_authorized=True, confidence=0.92, reasoning="ok")
    narrative = ExceptionNarrative(
        narrative_text="Rate 28.5 -> 30.0 per amendment.",
        citations=["sheet1!A12"],
        recommendation="ACCEPT",
        word_count=6,
    )
    check = AttributeCheck(control_id="DC-9", attribute_id="D", status="fail")

    result = run_investigation(
        check=check,
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-1",
        extraction_agent=_FakeExtractionAgent(extraction),  # type: ignore[arg-type]
        validation_agent=_FakeValidationAgent(validation),  # type: ignore[arg-type]
        narrative_agent=_FakeNarrativeAgent(narrative),  # type: ignore[arg-type]
        judge=_FakeJudge(verdict="fail", confidence=0.9),  # type: ignore[arg-type]
    )

    assert result["status"] == "escalated_to_human"
    assert result["judge_verdict"] == "fail"
