"""Unit tests for ``agentic_audit.layer3_agents.validation_agent`` and
the supervisor's ``validation_agent_node`` wiring — Step 7 task_05.

Five contracts pinned here:

1. The two prompt templates load and carry the structural markers
   the agent contract requires (substitution variables + the
   structured-output schema reference).
2. ``ValidationAgent`` short-circuits to the no-document fast path
   without calling the LLM, returning the deterministic
   ``is_authorized=False, confidence=0.9`` verdict. This is the
   dominant path once Step 8 wires real evidence and exists to
   keep cost down on the cheap negative.
3. ``ValidationAgent`` renders the correct per-exception-type prompt
   when the fast path does NOT short-circuit (a document was found),
   substituting scope + extracted-fact variables verbatim.
4. The LLM path returns a parsed ``ValidationFindings`` on a
   well-formed JSON response, retries once on parse / validation
   failure, and returns the deterministic fail-closed fallback
   (``is_authorized=False, confidence=0.0``) when both attempts fail.
5. The supervisor's ``validation_agent_node`` wires the injected
   agent through ``config["configurable"]["layer3_validation_agent"]``,
   falls back to no-op when absent (fail-closed default), and writes
   both ``validation_findings`` and a trace entry on success.

The real LLM call is exercised by the env-gated slow test in
``tests/integration/test_layer3_validation_agent_e2e.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.runnables import RunnableConfig

from agentic_audit.layer3_agents.state import (
    ExtractionFindings,
    InvestigationState,
    ValidationFindings,
)
from agentic_audit.layer3_agents.supervisor import (
    LAYER3_EXTRACTION_AGENT_CONFIG_KEY,
    LAYER3_VALIDATION_AGENT_CONFIG_KEY,
    run_investigation,
    validation_agent_node,
)
from agentic_audit.layer3_agents.validation_agent import (
    PROMPTS_DIR,
    ValidationAgent,
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
    extraction: ExtractionFindings | None = None,
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
        "validation_findings": None,
        "final_narrative": None,
        "judge_verdict": None,
        "judge_confidence": None,
        "confidence_score": 0.0,
        "iterations_used": 1,
        "status": "investigating",
    }


def _stub_client_returning_json(payload: dict[str, Any]) -> MagicMock:
    """Build a MagicMock standing in for ``openai.AzureOpenAI``.

    Returns a single completion whose ``message.content`` is the
    JSON-serialised ``payload``. Mirrors what the real client emits
    in JSON mode."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(payload)
    response.choices[0].finish_reason = "stop"
    client = MagicMock()
    client.chat.completions.create.return_value = response
    return client


# ── Prompt templates ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename",
    [
        "validation_v1_0_billing_rate_change.txt",
        "validation_v1_0_variance_plausibility.txt",
    ],
)
def test_prompt_template_loads_and_carries_required_markers(filename: str) -> None:
    """Every Validation prompt MUST:

    - exist on disk under the layer3 prompts dir
    - declare the four common substitution variables
    - reference ValidationFindings's three output keys
    - reject extra keys (so the LLM doesn't volunteer
      ``cited_evidence_fields`` and trip the ``extra="forbid"``
      validator)
    """
    text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
    for placeholder in (
        "${engagement_id}",
        "${control_id}",
        "${attribute_id}",
        "${quarter}",
    ):
        assert placeholder in text, f"{filename} missing placeholder {placeholder}"
    for key in ("is_authorized", "confidence", "reasoning"):
        assert key in text, f"{filename} missing schema key {key}"


def test_billing_rate_validation_prompt_documents_billing_subset_fields() -> None:
    text = (PROMPTS_DIR / "validation_v1_0_billing_rate_change.txt").read_text()
    for field in ("old_rate", "new_rate", "ima_amendment_text"):
        assert field in text, f"billing validation prompt missing field {field}"


def test_variance_validation_prompt_documents_variance_subset_fields() -> None:
    text = (PROMPTS_DIR / "validation_v1_0_variance_plausibility.txt").read_text()
    for field in ("variance_magnitude", "variance_explanation_text"):
        assert field in text, f"variance validation prompt missing field {field}"


# ── ValidationAgent — fast path (no LLM call) ────────────────────────


def test_fast_path_billing_rate_no_amendment_returns_unauthorized_high_confidence() -> None:
    """The dominant path: extraction reports no IMA amendment found.
    Validation must skip the LLM call entirely and return
    ``is_authorized=False, confidence=0.9``. Cheap and confident on
    the negative — the most common Layer-3 outcome."""
    extraction = ExtractionFindings(
        confidence=0.85,
        ima_amendment_found=False,
        old_rate=28.5,
        new_rate=30.0,
    )
    client = MagicMock()  # MUST NOT be called
    agent = ValidationAgent(endpoint="https://fake", client=client)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction)

    findings = agent.invoke(state)

    assert findings.is_authorized is False
    assert findings.confidence == pytest.approx(0.9)
    assert "No supporting document" in findings.reasoning
    client.chat.completions.create.assert_not_called()


def test_fast_path_variance_no_explanation_returns_unauthorized_high_confidence() -> None:
    """Variance fast path mirrors the billing path."""
    extraction = ExtractionFindings(
        confidence=0.85,
        variance_explanation_found=False,
        variance_magnitude=0.42,
    )
    client = MagicMock()
    agent = ValidationAgent(endpoint="https://fake", client=client)
    state = _state("variance_plausibility", "DC-2", "B", extraction=extraction)

    findings = agent.invoke(state)

    assert findings.is_authorized is False
    assert findings.confidence == pytest.approx(0.9)
    client.chat.completions.create.assert_not_called()


def test_fast_path_branches_on_exception_type() -> None:
    """The right ``found`` flag must be checked. A billing-rate
    investigation must NOT short-circuit because the variance flag is
    None (and vice-versa). Bug guard: in an early implementation an
    incorrect ``or`` would have fired the fast path on the wrong
    exception type."""
    # billing investigation, but extraction filled the variance subset
    # and left amendment_found unset (None). Should NOT fast-path.
    extraction = ExtractionFindings(confidence=0.5, variance_explanation_found=False)
    assert ValidationAgent._is_no_document_case(extraction, "billing_rate_change") is False


def test_invoke_raises_when_extraction_findings_missing() -> None:
    """Pre-condition: the supervisor only routes here after extraction
    lands. Calling validation on an empty extraction is a routing bug
    and must surface loudly, not silently proceed with an empty prompt."""
    agent = ValidationAgent(endpoint="https://fake", client=MagicMock())
    state = _state("billing_rate_change", "DC-9", "D", extraction=None)
    with pytest.raises(ValueError, match="extraction_findings"):
        agent.invoke(state)


# ── ValidationAgent — prompt rendering ───────────────────────────────


def test_renders_billing_rate_prompt_with_extracted_facts() -> None:
    extraction = ExtractionFindings(
        confidence=0.9,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_text="Effective Q3 2026, the management fee rate shall be 30.0 bps.",
    )
    agent = ValidationAgent(endpoint="https://fake", client=MagicMock())
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction)

    rendered = agent._peek_rendered_prompt(state)

    assert "${engagement_id}" not in rendered  # all placeholders substituted
    assert "eng-1" in rendered
    assert "DC-9" in rendered
    assert "Q3" in rendered
    assert "28.5" in rendered
    assert "30.0" in rendered
    assert "Effective Q3 2026" in rendered


def test_renders_variance_prompt_with_extracted_facts() -> None:
    extraction = ExtractionFindings(
        confidence=0.9,
        variance_explanation_found=True,
        variance_magnitude=0.42,
        variance_explanation_text="Q3 mandate change increased AUM by 38%.",
    )
    agent = ValidationAgent(endpoint="https://fake", client=MagicMock())
    state = _state("variance_plausibility", "DC-2", "B", extraction=extraction)

    rendered = agent._peek_rendered_prompt(state)

    assert "${engagement_id}" not in rendered
    assert "eng-1" in rendered
    assert "DC-2" in rendered
    assert "0.42" in rendered
    assert "mandate change" in rendered


def test_renders_unknown_optional_as_unknown_string() -> None:
    """Optional numeric fields that came back None from extraction
    render as ``"unknown"`` rather than the literal Python ``None``
    repr — gives the LLM a more useful signal."""
    extraction = ExtractionFindings(
        confidence=0.5,
        ima_amendment_found=True,
        old_rate=None,
        new_rate=30.0,
        ima_amendment_text="Some text.",
    )
    agent = ValidationAgent(endpoint="https://fake", client=MagicMock())
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction)
    rendered = agent._peek_rendered_prompt(state)
    assert "unknown" in rendered
    assert "None" not in rendered.split("Sufficiency")[0]  # not in the facts block


# ── ValidationAgent — LLM path (well-formed response) ────────────────


def test_llm_path_returns_authorized_on_sufficient_amendment() -> None:
    """Happy LLM path — well-formed JSON parses cleanly into
    ``ValidationFindings`` and surfaces the agent's verdict."""
    extraction = ExtractionFindings(
        confidence=0.9,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_text="Effective Q3 2026, the management fee rate shall be 30.0 bps.",
    )
    client = _stub_client_returning_json(
        {
            "is_authorized": True,
            "confidence": 0.92,
            "reasoning": "The amendment effective Q3 2026 explicitly authorises 30.0 bps.",
        }
    )
    agent = ValidationAgent(endpoint="https://fake", client=client)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction)

    findings = agent.invoke(state)

    assert findings.is_authorized is True
    assert findings.confidence > 0.8
    assert "Q3 2026" in findings.reasoning
    client.chat.completions.create.assert_called_once()


def test_llm_path_returns_unauthorized_on_implausible_variance() -> None:
    extraction = ExtractionFindings(
        confidence=0.9,
        variance_explanation_found=True,
        variance_magnitude=0.42,
        variance_explanation_text="Normal fluctuation; no material change.",
    )
    client = _stub_client_returning_json(
        {
            "is_authorized": False,
            "confidence": 0.78,
            "reasoning": "A 42% variance is not explained by 'normal fluctuation'.",
        }
    )
    agent = ValidationAgent(endpoint="https://fake", client=client)
    state = _state("variance_plausibility", "DC-2", "B", extraction=extraction)

    findings = agent.invoke(state)

    assert findings.is_authorized is False
    assert findings.confidence > 0.7
    assert "42%" in findings.reasoning


# ── ValidationAgent — failure modes ──────────────────────────────────


def test_llm_path_retries_on_parse_failure_then_succeeds() -> None:
    """Parse failure on attempt 1, well-formed JSON on attempt 2 →
    surface the parsed verdict. Mirrors the Layer-2 ``Judge``'s
    retry posture."""
    extraction = ExtractionFindings(
        confidence=0.9,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_text="text",
    )
    bad_response = MagicMock()
    bad_response.choices = [MagicMock()]
    bad_response.choices[0].message.content = "not json {"
    bad_response.choices[0].finish_reason = "stop"
    good_response = MagicMock()
    good_response.choices = [MagicMock()]
    good_response.choices[0].message.content = json.dumps(
        {"is_authorized": True, "confidence": 0.85, "reasoning": "ok."}
    )
    good_response.choices[0].finish_reason = "stop"
    client = MagicMock()
    client.chat.completions.create.side_effect = [bad_response, good_response]
    agent = ValidationAgent(endpoint="https://fake", client=client)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction)

    findings = agent.invoke(state)

    assert findings.is_authorized is True
    assert client.chat.completions.create.call_count == 2


def test_llm_path_falls_back_to_unauthorized_after_two_failures() -> None:
    """Two consecutive parse failures → deterministic fail-closed
    fallback. ValidationAgent must NEVER raise into the supervisor —
    a fail-closed verdict with diagnostic reasoning is the contract."""
    extraction = ExtractionFindings(
        confidence=0.9,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_text="text",
    )
    bad_response = MagicMock()
    bad_response.choices = [MagicMock()]
    bad_response.choices[0].message.content = "still not json"
    bad_response.choices[0].finish_reason = "stop"
    client = MagicMock()
    client.chat.completions.create.side_effect = [bad_response, bad_response]
    agent = ValidationAgent(endpoint="https://fake", client=client)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction)

    findings = agent.invoke(state)

    assert findings.is_authorized is False
    assert findings.confidence == 0.0
    assert "Validation failure" in findings.reasoning
    assert client.chat.completions.create.call_count == 2


def test_llm_path_falls_back_on_validation_error_after_two_attempts() -> None:
    """JSON parses but ``ValidationFindings`` rejects it (e.g. extra
    field). Retry once; second failure → fail-closed fallback."""
    extraction = ExtractionFindings(
        confidence=0.9,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_text="text",
    )
    bad_response = MagicMock()
    bad_response.choices = [MagicMock()]
    bad_response.choices[0].message.content = json.dumps(
        {"is_authorized": True, "confidence": 1.5, "reasoning": "ok."}  # confidence out of range
    )
    bad_response.choices[0].finish_reason = "stop"
    client = MagicMock()
    client.chat.completions.create.side_effect = [bad_response, bad_response]
    agent = ValidationAgent(endpoint="https://fake", client=client)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction)

    findings = agent.invoke(state)

    assert findings.is_authorized is False
    assert findings.confidence == 0.0


# ── validation_agent_node — supervisor wiring ────────────────────────


class _FakeValidationAgent:
    """Quack-typed stand-in for ``ValidationAgent`` — implements only
    ``invoke``. Lets the supervisor test bypass openai entirely."""

    def __init__(self, findings: ValidationFindings) -> None:
        self._findings = findings
        self.calls: list[InvestigationState] = []

    def invoke(self, state: InvestigationState) -> ValidationFindings:
        self.calls.append(state)
        return self._findings


def _config(agent: Any = None) -> RunnableConfig:
    return {
        "configurable": ({LAYER3_VALIDATION_AGENT_CONFIG_KEY: agent} if agent is not None else {})
    }


def test_validation_agent_node_runs_injected_agent() -> None:
    findings = ValidationFindings(is_authorized=True, confidence=0.88, reasoning="ok.")
    fake = _FakeValidationAgent(findings)
    extraction = ExtractionFindings(confidence=0.9, ima_amendment_found=True)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction)

    updates = validation_agent_node(state, _config(agent=fake))

    assert updates["validation_findings"] is findings
    assert len(fake.calls) == 1
    assert fake.calls[0]["exception_type"] == "billing_rate_change"


def test_validation_agent_node_appends_trace_entry() -> None:
    fake = _FakeValidationAgent(
        ValidationFindings(is_authorized=False, confidence=0.9, reasoning="No doc.")
    )
    extraction = ExtractionFindings(confidence=0.9, ima_amendment_found=False)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction)

    updates = validation_agent_node(state, _config(agent=fake))

    assert len(updates["investigation_log"]) == 1
    step = updates["investigation_log"][0]
    assert step.actor == "validation_agent"
    assert step.action == "emitted_validation_findings"


def test_validation_agent_node_no_op_emits_iter_increment_and_log_entry() -> None:
    """Fail-closed default — no agent injected. The node still emits a
    state delta (iter increment + ``no_agent_injected_no_op`` log
    entry) so the LangGraph super-step engine doesn't declare early
    convergence on a no-state-change return."""
    extraction = ExtractionFindings(confidence=0.9, ima_amendment_found=True)
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction)
    updates = validation_agent_node(state, _config())
    assert updates["iterations_used"] == state["iterations_used"] + 1  # type: ignore[operator]
    step = updates["investigation_log"][0]
    assert step.actor == "validation_agent"
    assert step.action == "no_agent_injected_no_op"
    assert "validation_findings" not in updates


# ── End-to-end via run_investigation ─────────────────────────────────


class _FakeExtractionAgent:
    """Minimal stand-in mirroring the test_extraction_agent.py helper."""

    def __init__(self, findings: ExtractionFindings) -> None:
        self._findings = findings
        self.calls: list[InvestigationState] = []

    def invoke(self, state: InvestigationState) -> ExtractionFindings:
        self.calls.append(state)
        return self._findings


def test_run_investigation_threads_validation_agent_through_config() -> None:
    """The injected ValidationAgent must reach the node via the
    configurable dict. With the narrative node still a stub, one
    iteration each populates extraction + validation, then the
    supervisor routes to narrative (None) and eventually escalates
    at the iteration cap."""
    extraction = ExtractionFindings(
        confidence=0.9,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
    )
    fake_extraction = _FakeExtractionAgent(extraction)
    fake_validation = _FakeValidationAgent(
        ValidationFindings(is_authorized=True, confidence=0.92, reasoning="ok.")
    )
    check = AttributeCheck(control_id="DC-9", attribute_id="D", status="fail")

    result = run_investigation(
        check=check,
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-1",
        extraction_agent=fake_extraction,  # type: ignore[arg-type]
        validation_agent=fake_validation,  # type: ignore[arg-type]
    )

    assert len(fake_extraction.calls) == 1
    assert len(fake_validation.calls) == 1
    assert result["extraction_findings"] is not None
    assert result["validation_findings"] is not None
    assert result["validation_findings"].is_authorized is True  # type: ignore[union-attr]
    # Narrative still stub => still escalates at cap.
    assert result["status"] == "escalated_to_human"


def test_run_investigation_validation_only_without_extraction_loops_to_cap() -> None:
    """Regression guard: if a caller wires only validation but not
    extraction, the supervisor still routes to extraction first
    (which no-ops), so validation never runs and the cap fires
    cleanly."""
    fake_validation = _FakeValidationAgent(
        ValidationFindings(is_authorized=True, confidence=0.92, reasoning="ok.")
    )
    check = AttributeCheck(control_id="DC-9", attribute_id="D", status="fail")
    result = run_investigation(
        check=check,
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-1",
        validation_agent=fake_validation,  # type: ignore[arg-type]
    )
    assert result["status"] == "escalated_to_human"
    assert result["extraction_findings"] is None
    assert result["validation_findings"] is None
    assert len(fake_validation.calls) == 0


def test_validation_node_config_key_is_distinct_from_extraction() -> None:
    """Sanity guard against a refactor accidentally collapsing the two
    config keys onto the same string — the Layer 2 cost-telemetry
    follow-up did exactly this on a different shape and cost half a
    PR to unwind."""
    assert LAYER3_VALIDATION_AGENT_CONFIG_KEY != LAYER3_EXTRACTION_AGENT_CONFIG_KEY
