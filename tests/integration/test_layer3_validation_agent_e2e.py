"""Live Azure OpenAI integration test for the Validation sub-agent
(Step 7 task_05).

Marked ``@pytest.mark.slow`` and gated on ``AZURE_OPENAI_ENDPOINT``.
CI's default unit-test pass skips this. Run on demand with::

    export AZURE_OPENAI_ENDPOINT=https://aoai-aaf-rbpal-dev.openai.azure.com/
    poetry run pytest -m slow tests/integration/test_layer3_validation_agent_e2e.py -v -s

Authentication: ``DefaultAzureCredential`` resolves the auth chain —
locally that's a cached ``az login`` token, on Databricks compute
that's the job MSI. No API keys.

What this test verifies:

1. ``openai.AzureOpenAI`` (raw client, not LangChain) authenticates
   against the tenant's deployment (auth chain reaches Cognitive
   Services).
2. The validation prompt renders with extracted facts substituted in
   and the LLM returns valid JSON parseable as ``ValidationFindings``.
3. The fast path returns immediately without an LLM call when
   extraction reports no document.
4. A sufficient-amendment scenario yields ``is_authorized=True`` with
   reasonable confidence; an implausible-variance scenario yields
   ``is_authorized=False``.

What it does NOT verify (out of scope — task_06 / task_07):

- The full supervisor → extraction → validation → narrative loop
  (task_07 wires a happy-path test with all sub-agents live).
- Cost telemetry recording (task_08 plumbs UsageRecorder).

Cost per run: ~$0.005–0.015 (two single-call LLM completions plus
one fast-path scenario that doesn't call the LLM at all). Idempotent
— safe to re-run.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from agentic_audit.layer3_agents.state import (
    ExtractionFindings,
    InvestigationState,
    ValidationFindings,
)
from agentic_audit.layer3_agents.validation_agent import ValidationAgent
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
def validation_agent() -> ValidationAgent:
    """Build a ValidationAgent from env vars, skipping if the endpoint
    isn't configured.

    Same DefaultAzureCredential chain as Layer 2's generator + judge —
    a stale ``az login`` will surface as an AAD error inside the
    chat-completions call with a clear remediation path."""
    if not _have_azure_openai_endpoint():
        pytest.skip(
            "AZURE_OPENAI_ENDPOINT not set; skipping live integration test. "
            "Export e.g. AZURE_OPENAI_ENDPOINT=https://aoai-aaf-rbpal-dev.openai.azure.com/ "
            "and ensure `az login` is active."
        )
    return ValidationAgent.from_env()


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


def _state(
    exception_type: str,
    control_id: str,
    attribute_id: str,
    extraction: ExtractionFindings,
) -> InvestigationState:
    return {
        "investigation_run_id": "inv-live-e2e",
        "agent_run_id": "sweep-live-e2e",
        "engagement_id": "eng-live-e2e",
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
        "iterations_used": 2,
        "status": "investigating",
    }


def _print_outcome(label: str, findings: ValidationFindings, raw: str | None) -> None:
    """Operator-friendly diagnostic dump.

    Mirrors the `_invoke_with_messages` print pattern from the
    Extraction agent's slow test — surfaces the LLM's raw JSON
    alongside the parsed verdict so a developer running this on
    demand can see both."""
    print(f"\n=== {label} ===")
    print(f"is_authorized: {findings.is_authorized}")
    print(f"confidence:    {findings.confidence}")
    print(f"reasoning:     {findings.reasoning}")
    if raw is not None:
        print(f"raw LLM JSON:  {raw}")
    else:
        print("raw LLM JSON:  <fast path — no LLM call>")
    print()


def test_validation_fast_path_skips_llm_call(
    validation_agent: ValidationAgent,
) -> None:
    """The fast path is the cheap-and-confident negative — no LLM
    call, deterministic verdict. This is the most common Layer-3
    outcome once Step 8 wires real evidence, so verifying it short-
    circuits before the network call is the load-bearing assertion."""
    extraction = ExtractionFindings(
        confidence=0.9,
        ima_amendment_found=False,  # triggers fast path
        old_rate=28.5,
        new_rate=30.0,
    )
    state = _state("billing_rate_change", "DC-9", "D", extraction)

    findings, raw = validation_agent._invoke_with_raw_response(state)

    _print_outcome("Fast path: no IMA amendment", findings, raw)

    assert findings.is_authorized is False
    assert findings.confidence == pytest.approx(0.9)
    assert raw is None  # proof no LLM call fired


def test_validation_authorizes_sufficient_amendment(
    validation_agent: ValidationAgent,
) -> None:
    """A clearly-cited amendment with matching rate + valid effective
    date should land at is_authorized=True with confidence >= 0.7.

    Uses a synthetic IMA-amendment text that satisfies all three
    sufficiency criteria from the prompt:
      1. References a billing-rate change.
      2. Authorises the matching new_rate (30.0 bps).
      3. Effective date precedes Q3 (2026-06-15 → before Q3 start)."""
    extraction = ExtractionFindings(
        confidence=0.92,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_text=(
            "Amendment dated 2026-06-15 to the Investment Management "
            "Agreement: effective Q3 2026, the management fee rate "
            "shall be increased from 28.5 bps to 30.0 bps. Signed "
            "by both parties on 2026-06-15."
        ),
        evidence_anchors=["sheet1!A12", "amendment.pdf!p2"],
    )
    state = _state("billing_rate_change", "DC-9", "D", extraction)

    findings, raw = validation_agent._invoke_with_raw_response(state)

    _print_outcome("LLM path: sufficient amendment", findings, raw)

    assert isinstance(findings, ValidationFindings)
    assert raw is not None  # LLM was called
    assert findings.is_authorized is True
    assert findings.confidence > 0.7
    assert len(findings.reasoning) > 10  # not a placeholder


def test_validation_rejects_implausible_variance(
    validation_agent: ValidationAgent,
) -> None:
    """A 42% variance with a "normal fluctuation" explanation must
    fail plausibility — generic language is explicitly called out as
    insufficient in the prompt."""
    extraction = ExtractionFindings(
        confidence=0.88,
        variance_explanation_found=True,
        variance_magnitude=0.42,
        variance_explanation_text=(
            "Normal fluctuation in the period; no material change to the underlying methodology."
        ),
        evidence_anchors=["sheet2!B7"],
    )
    state = _state("variance_plausibility", "DC-2", "B", extraction)

    findings, raw = validation_agent._invoke_with_raw_response(state)

    _print_outcome("LLM path: implausible variance", findings, raw)

    assert isinstance(findings, ValidationFindings)
    assert raw is not None
    assert findings.is_authorized is False
    assert findings.confidence > 0.5
    assert len(findings.reasoning) > 10
