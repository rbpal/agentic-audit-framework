"""Unit tests for ``agentic_audit.layer3_agents.extraction_agent`` and
the placeholder tools — task_04.

Four contracts pinned here:

1. The three placeholder tools (``read_billing_rate``,
   ``compare_billing_rates``, ``read_reviewer_comments``) are
   ``@tool``-decorated with the right names + invokable arg signatures.
   The tool decoration is what ``create_react_agent`` introspects; a
   missing or mistyped decorator surfaces only at LLM runtime, so
   pinning here keeps the test loop fast.
2. The two prompt templates load and contain the structural markers
   the agent contract requires (the substitution variables + the
   structured-output instruction).
3. ``ExtractionAgent`` renders the correct per-exception-type
   prompt and substitutes scope variables verbatim. The prompt-
   selection logic is the load-bearing piece of routing — wrong
   prompt = wrong subset populated = wrong narrative downstream.
4. The supervisor's ``extraction_agent_node`` wires the injected
   agent through ``config["configurable"]["layer3_extraction_agent"]``,
   falls back to no-op when absent (fail-closed default that lets the
   3-iteration cap eventually escalate), and writes both
   ``extraction_findings`` and a trace entry on success.

The real LLM loop is exercised by the env-gated slow test in
``tests/integration/test_layer3_extraction_agent_e2e.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from agentic_audit.layer3_agents.extraction_agent import (
    PROMPTS_DIR,
    ExtractionAgent,
)
from agentic_audit.layer3_agents.state import (
    ExtractionFindings,
    InvestigationState,
)
from agentic_audit.layer3_agents.supervisor import (
    LAYER3_EXTRACTION_AGENT_CONFIG_KEY,
    extraction_agent_node,
    run_investigation,
)
from agentic_audit.layer3_agents.tools import (
    compare_billing_rates,
    read_billing_rate,
    read_reviewer_comments,
)

# Imports kept even where the matching shape test moved to test_tools.py:
# they're still referenced by test_tools_are_langchain_tool_instances
# below (the @tool-decoration parametrize).
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


def _state(exception_type: str, control_id: str, attribute_id: str) -> InvestigationState:
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
        "extraction_findings": None,
        "validation_findings": None,
        "final_narrative": None,
        "judge_verdict": None,
        "judge_confidence": None,
        "confidence_score": 0.0,
        "iterations_used": 1,
        "status": "investigating",
    }


# ── Placeholder tools ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tool_obj",
    [read_billing_rate, compare_billing_rates, read_reviewer_comments],
)
def test_tools_are_langchain_tool_instances(tool_obj: BaseTool) -> None:
    """The @tool decorator wraps each function in a BaseTool subclass.
    create_react_agent introspects this — pinning the decoration here
    catches a refactor that accidentally drops it."""
    assert isinstance(tool_obj, BaseTool)
    assert tool_obj.name
    assert tool_obj.description


# NOTE: All three Layer-3 tool shape + behaviour tests live in
# tests/unit/layer3_agents/test_tools.py as of Step 8 task_03.
# All three tools require InjectedState now; direct .invoke({...})
# without state no longer works. No tool-specific tests remain here —
# only the @tool-decoration parametrize above (which checks the
# BaseTool wrapping survives) is tool-specific.


# ── Prompt templates ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename",
    [
        "extraction_v1_0_billing_rate_change.txt",
        "extraction_v1_0_variance_plausibility.txt",
    ],
)
def test_prompt_template_loads_and_carries_required_markers(filename: str) -> None:
    """Every Extraction prompt MUST:

    - exist on disk under the layer3 prompts dir
    - declare the five substitution variables the agent renders
    - reference ExtractionFindings as the structured-output target

    A missing variable = a render-time KeyError when the agent runs;
    a missing schema reference = the LLM returning free-form JSON
    that fails pydantic validation. Both are bugs that should not
    reach production."""
    text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
    for placeholder in (
        "${engagement_id}",
        "${control_id}",
        "${attribute_id}",
        "${quarter}",
        "${prior_quarter}",
    ):
        assert placeholder in text, f"{filename} missing placeholder {placeholder}"
    assert "ExtractionFindings" in text
    assert "evidence_anchors" in text
    assert "confidence" in text


def test_billing_rate_prompt_documents_billing_subset_fields() -> None:
    text = (PROMPTS_DIR / "extraction_v1_0_billing_rate_change.txt").read_text()
    for field in ("old_rate", "new_rate", "ima_amendment_found", "ima_amendment_text"):
        assert field in text, f"billing prompt missing field {field}"


def test_variance_prompt_documents_variance_subset_fields() -> None:
    text = (PROMPTS_DIR / "extraction_v1_0_variance_plausibility.txt").read_text()
    for field in (
        "variance_magnitude",
        "variance_explanation_found",
        "variance_explanation_text",
    ):
        assert field in text, f"variance prompt missing field {field}"


# ── ExtractionAgent — prompt rendering ───────────────────────────────


def test_extraction_agent_renders_billing_rate_prompt() -> None:
    """Prompt selection by exception_type is the load-bearing routing
    decision. Wrong prompt = wrong subset populated = wrong narrative
    downstream."""
    agent = ExtractionAgent(endpoint="https://fake", deployment="gpt-4o")
    state = _state("billing_rate_change", "DC-9", "D")
    rendered = agent._peek_rendered_prompt(state)
    assert "billing_rate_change" in rendered or "IMA" in rendered
    assert "${engagement_id}" not in rendered  # all placeholders substituted
    assert "eng-1" in rendered
    assert "DC-9" in rendered
    assert "Q3" in rendered
    assert "Q2" in rendered  # prior quarter
    assert "old_rate" in rendered


def test_extraction_agent_renders_variance_plausibility_prompt() -> None:
    agent = ExtractionAgent(endpoint="https://fake", deployment="gpt-4o")
    state = _state("variance_plausibility", "DC-2", "B")
    rendered = agent._peek_rendered_prompt(state)
    assert "variance" in rendered.lower()
    assert "${engagement_id}" not in rendered
    assert "eng-1" in rendered
    assert "DC-2" in rendered
    assert "variance_magnitude" in rendered


def test_extraction_agent_raises_on_unknown_exception_type() -> None:
    """Defensive: a future exception_type added to the Literal but not
    to the prompts directory must surface as FileNotFoundError, not a
    silent fallback to an arbitrary prompt."""
    agent = ExtractionAgent(endpoint="https://fake", deployment="gpt-4o")
    state = _state("billing_rate_change", "DC-9", "D")
    # Force a non-existent exception_type past the Literal check.
    state["exception_type"] = "future_unknown_type"  # type: ignore[typeddict-item]
    with pytest.raises(FileNotFoundError):
        agent._peek_rendered_prompt(state)


# ── ExtractionAgent — response parsing ───────────────────────────────


def test_parse_structured_response_accepts_pydantic_instance() -> None:
    findings = ExtractionFindings(confidence=0.9, ima_amendment_found=True)
    result = {"structured_response": findings, "messages": []}
    parsed = ExtractionAgent._parse_structured_response(result)
    assert parsed is findings


def test_parse_structured_response_revalidates_dict() -> None:
    """LangGraph minor versions can return raw dicts in
    structured_response. The defensive re-validation must
    reconstruct the model rather than passing the dict through."""
    result = {
        "structured_response": {"confidence": 0.8, "ima_amendment_found": False},
        "messages": [],
    }
    parsed = ExtractionAgent._parse_structured_response(result)
    assert isinstance(parsed, ExtractionFindings)
    assert parsed.confidence == 0.8


def test_parse_structured_response_raises_on_missing_key() -> None:
    with pytest.raises(ValueError, match="structured_response"):
        ExtractionAgent._parse_structured_response({"messages": []})


def test_parse_structured_response_raises_on_invalid_dict() -> None:
    with pytest.raises(ValueError, match="ExtractionFindings"):
        ExtractionAgent._parse_structured_response(
            {"structured_response": {"confidence": 999}, "messages": []}
        )


# ── extraction_agent_node — supervisor wiring ────────────────────────


class _FakeExtractionAgent:
    """Quack-typed stand-in for ``ExtractionAgent`` — implements only
    the ``invoke`` surface the node touches. Lets the supervisor test
    bypass langchain entirely."""

    def __init__(self, findings: ExtractionFindings) -> None:
        self._findings = findings
        self.calls: list[InvestigationState] = []

    def invoke(self, state: InvestigationState) -> ExtractionFindings:
        self.calls.append(state)
        return self._findings


def _config(agent: Any = None) -> RunnableConfig:
    return {
        "configurable": ({LAYER3_EXTRACTION_AGENT_CONFIG_KEY: agent} if agent is not None else {})
    }


def test_extraction_agent_node_runs_injected_agent() -> None:
    findings = ExtractionFindings(confidence=0.85, ima_amendment_found=True)
    fake = _FakeExtractionAgent(findings)
    state = _state("billing_rate_change", "DC-9", "D")
    updates = extraction_agent_node(state, _config(agent=fake))
    assert updates["extraction_findings"] is findings
    assert len(fake.calls) == 1
    assert fake.calls[0]["exception_type"] == "billing_rate_change"


def test_extraction_agent_node_appends_trace_entry() -> None:
    """Sub-agent trace entries are the audit lineage — they have to
    name the actor + action so the persisted tool_trace lets a
    reviewer follow the chain."""
    fake = _FakeExtractionAgent(ExtractionFindings(confidence=0.85))
    state = _state("billing_rate_change", "DC-9", "D")
    updates = extraction_agent_node(state, _config(agent=fake))
    assert len(updates["investigation_log"]) == 1
    step = updates["investigation_log"][0]
    assert step.actor == "extraction_agent"
    assert step.action == "emitted_extraction_findings"


def test_extraction_agent_node_no_op_emits_iter_increment_and_log_entry() -> None:
    """Fail-closed default — no agent injected. The node still emits a
    state delta (iter increment + ``no_agent_injected_no_op`` log
    entry) so the LangGraph super-step engine doesn't declare early
    convergence on a no-state-change return. The supervisor's
    iteration cap is what eventually escalates the loop."""
    state = _state("billing_rate_change", "DC-9", "D")
    updates = extraction_agent_node(state, _config())
    assert updates["iterations_used"] == state["iterations_used"] + 1  # type: ignore[operator]
    assert len(updates["investigation_log"]) == 1
    step = updates["investigation_log"][0]
    assert step.actor == "extraction_agent"
    assert step.action == "no_agent_injected_no_op"
    assert "extraction_findings" not in updates


# ── End-to-end via run_investigation ─────────────────────────────────


def test_run_investigation_threads_extraction_agent_through_config() -> None:
    """The injected ExtractionAgent must reach the node via the
    configurable dict. With the validation + narrative nodes still
    stubs, one iteration populates extraction_findings then the
    supervisor routes to validation (None) and eventually escalates
    at the cap."""
    fake = _FakeExtractionAgent(ExtractionFindings(confidence=0.9, ima_amendment_found=True))
    check = AttributeCheck(control_id="DC-9", attribute_id="D", status="fail")
    result = run_investigation(
        check=check,
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-1",
        extraction_agent=fake,  # type: ignore[arg-type]
    )
    assert len(fake.calls) == 1
    assert result["extraction_findings"] is not None
    assert result["extraction_findings"].ima_amendment_found is True  # type: ignore[union-attr]
    # Validation + narrative still stubs => still escalates at cap.
    assert result["status"] == "escalated_to_human"


def test_run_investigation_without_extraction_agent_loops_to_cap() -> None:
    """Regression guard for the prior task_03 behaviour. Without an
    injected agent the iteration cap still fires and produces a
    clean escalate."""
    check = AttributeCheck(control_id="DC-9", attribute_id="D", status="fail")
    result = run_investigation(
        check=check,
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-1",
    )
    assert result["status"] == "escalated_to_human"
    assert result["extraction_findings"] is None
    assert result["iterations_used"] == 3
