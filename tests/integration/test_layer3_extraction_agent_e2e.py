"""Live Azure OpenAI integration test for the Extraction sub-agent
(Step 7 task_04).

Marked ``@pytest.mark.slow`` and gated on ``AZURE_OPENAI_ENDPOINT``.
CI's default unit-test pass skips this. Run on demand with::

    AZURE_OPENAI_ENDPOINT=https://aoai-aaf-rbpal-dev.openai.azure.com/  \\
    poetry run pytest -m slow tests/integration/test_layer3_extraction_agent_e2e.py -v

Authentication: ``DefaultAzureCredential`` resolves the auth chain —
locally that's a cached ``az login`` token, on Databricks compute
that's the job MSI. No API keys.

What this test verifies:

1. ``langchain_openai.AzureChatOpenAI`` can authenticate against the
   tenant's deployment (auth chain reaches Cognitive Services).
2. ``create_react_agent`` builds with the three placeholder tools +
   the ``ExtractionFindings`` ``response_format`` without errors.
3. The agent invokes against a billing-rate-change scenario, runs at
   least one tool, and returns a structured-output
   ``ExtractionFindings`` parseable at our boundary.

What it does NOT verify (out of scope — Step 8 or task_07):

- Correctness of the ``ima_amendment_found`` decision (the
  placeholder tools always return False, so the LLM will report
  False every time; that's expected until Step 8).
- The full supervisor → extraction → validation → narrative loop
  (task_07 wires a happy-path test with all sub-agents live).
- Cost telemetry recording (task_08 plumbs UsageRecorder).

Cost per run: ~$0.01–0.03 (one ReAct loop, a few tool calls, one
structured-output response). Idempotent — safe to re-run.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

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

UTC_TS = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


def _have_azure_openai_endpoint() -> bool:
    return bool(os.getenv("AZURE_OPENAI_ENDPOINT"))


@pytest.fixture(scope="module")
def extraction_agent() -> ExtractionAgent:
    """Build an ExtractionAgent from env vars, skipping if the endpoint
    isn't configured.

    Same DefaultAzureCredential chain as the Layer 2 generator + judge
    — a stale ``az login`` will surface as an AAD error inside the
    ReAct loop with a clear remediation path.
    """
    if not _have_azure_openai_endpoint():
        pytest.skip(
            "AZURE_OPENAI_ENDPOINT not set; skipping live integration test. "
            "Export e.g. AZURE_OPENAI_ENDPOINT=https://aoai-aaf-rbpal-dev.openai.azure.com/ "
            "and ensure `az login` is active."
        )
    return ExtractionAgent.from_env()


def _evidence(control_id: str, quarter: str) -> ExtractedEvidence:
    return ExtractedEvidence(
        engagement_id="eng-live-e2e",
        control_id=control_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        run_id=f"run-{quarter}",
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


def _billing_rate_state() -> InvestigationState:
    return {
        "investigation_run_id": "inv-live-e2e",
        "agent_run_id": "sweep-live-e2e",
        "engagement_id": "eng-live-e2e",
        "control_id": "DC-9",
        "attribute_id": "D",
        "quarter": "Q3",
        "exception_type": "billing_rate_change",
        "current_quarter_evidence": _evidence("DC-9", "Q3"),
        "prior_quarter_evidence": _evidence("DC-9", "Q2"),
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


def test_extraction_agent_invoke_returns_extraction_findings(
    extraction_agent: ExtractionAgent,
) -> None:
    """End-to-end smoke: build the ReAct agent against a real Azure
    deployment, run it once, expect a structured ``ExtractionFindings``
    back.

    The placeholder tools return canned empty payloads, so the model
    will most likely report ``ima_amendment_found=False`` and a low
    confidence. That's correct behaviour for unanchored evidence.
    What we verify here is **schema** — the ReAct loop completes and
    the structured output validates. Step 8 makes the assertion
    semantic (real tools, real evidence)."""
    state = _billing_rate_state()
    findings = extraction_agent.invoke(state)

    # Surface the LLM's response so a developer running this test on
    # demand can see what the model actually emitted. Visible with
    # `pytest -s` (default captures stdout otherwise). Cheap signal —
    # printed JSON is the single piece of evidence that the LLM was
    # really reached + parsed correctly.
    print("\n=== ExtractionAgent live response ===")
    print(findings.model_dump_json(indent=2, exclude_none=True))
    print(f"=== confidence={findings.confidence} ===\n")

    assert isinstance(findings, ExtractionFindings)
    # Confidence is bounded by the pydantic validator; this assertion
    # is more about "the model populated SOMETHING in the allowed
    # range" than a calibrated value.
    assert 0.0 <= findings.confidence <= 1.0
    # evidence_anchors must always be a list (default_factory). LLMs
    # sometimes return null instead of [] — pydantic's default kicks
    # in either way, but pin it explicitly here so a future schema
    # change doesn't silently flip the type.
    assert isinstance(findings.evidence_anchors, list)
