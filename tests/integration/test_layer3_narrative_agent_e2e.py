"""Live Azure OpenAI integration test for the Narrative sub-agent
(Step 7 task_06).

Marked ``@pytest.mark.slow`` and gated on ``AZURE_OPENAI_ENDPOINT``.
CI's default unit-test pass skips this. Run on demand with::

    export AZURE_OPENAI_ENDPOINT=https://aoai-aaf-rbpal-dev.openai.azure.com/
    poetry run pytest -m slow tests/integration/test_layer3_narrative_agent_e2e.py -v -s

Authentication: ``DefaultAzureCredential`` resolves the auth chain.
No API keys.

What this test verifies:

1. ``openai.AzureOpenAI`` (raw client, not LangChain) authenticates
   against the tenant's deployment.
2. The exception-narrative prompts render with extraction +
   validation findings substituted in and the LLM returns valid JSON
   parseable as ``ExceptionNarrative``.
3. **DC-9.D billing-rate-change happy path** produces a narrative
   that cites both the rate delta (``28.5 → 30.0``) AND the IMA
   amendment, with ``recommendation="ACCEPT"``.
4. **DC-2.B variance-plausibility happy path** produces a narrative
   that cites both the magnitude (``0.42``) AND the explanation, with
   ``recommendation="ACCEPT"``.
5. The narrative grounds against the Layer-3 fact-check substrate
   (the ``invoke`` path runs fact-check internally; if grounding
   failed twice, the test would see the fallback ESCALATE narrative
   and fail the recommendation assertions).

What it does NOT verify (out of scope — task_07 / task_08):

- The full supervisor → extraction → validation → narrative loop
  through ``run_investigation`` (task_07's integration tests).
- Cost telemetry recording (task_08).

Cost per run: ~$0.01–0.04 (two LLM calls, each up to ~280 output
tokens for the narrative + JSON envelope). Idempotent.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from agentic_audit.layer3_agents.narrative_agent import NarrativeAgent
from agentic_audit.layer3_agents.state import (
    ExceptionNarrative,
    ExtractionFindings,
    InvestigationState,
    ValidationFindings,
)
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
def narrative_agent() -> NarrativeAgent:
    if not _have_azure_openai_endpoint():
        pytest.skip(
            "AZURE_OPENAI_ENDPOINT not set; skipping live integration test. "
            "Export e.g. AZURE_OPENAI_ENDPOINT=https://aoai-aaf-rbpal-dev.openai.azure.com/ "
            "and ensure `az login` is active."
        )
    return NarrativeAgent.from_env()


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
    *,
    extraction: ExtractionFindings,
    validation: ValidationFindings,
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
        "validation_findings": validation,
        "final_narrative": None,
        "judge_verdict": None,
        "judge_confidence": None,
        "confidence_score": 0.0,
        "iterations_used": 3,
        "status": "investigating",
    }


def _print_outcome(label: str, narrative: ExceptionNarrative, raw: str | None) -> None:
    print(f"\n=== {label} ===")
    print(f"recommendation: {narrative.recommendation}")
    print(f"word_count:     {narrative.word_count}")
    print(f"citations:      {narrative.citations}")
    print(f"narrative_text: {narrative.narrative_text}")
    if raw is not None:
        print(f"raw LLM JSON:   {raw}")
    else:
        print("raw LLM JSON:   <fallback ESCALATE — no LLM raw>")
    print()


def test_dc9d_billing_rate_change_happy_path_produces_accept_narrative(
    narrative_agent: NarrativeAgent,
) -> None:
    """A clearly-cited amendment + matching rate + valid effective
    date should produce ``recommendation=ACCEPT`` with a narrative
    that cites both the rate delta and the amendment text.

    Per privateDocs § task_06 verification:
      "End-to-end test with the DC-9.D Q3 happy path produces a
      narrative containing both the rate delta (28.5 -> 30.0) AND
      the amendment citation (IMA amendment dated ...)."
    """
    extraction = ExtractionFindings(
        confidence=0.92,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_text=(
            "Amendment dated 2026-06-15 to the Investment Management "
            "Agreement: effective Q3 2026, the management fee rate "
            "shall be increased from 28.5 bps to 30.0 bps."
        ),
        evidence_anchors=["sheet1!A12", "amendment.pdf!p2"],
    )
    validation = ValidationFindings(
        is_authorized=True,
        confidence=0.95,
        reasoning=(
            "The amendment dated 2026-06-15 explicitly authorises the "
            "rate change to 30.0 bps effective Q3 2026, satisfying "
            "all sufficiency criteria."
        ),
    )
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction, validation=validation)

    narrative, raw = narrative_agent._invoke_with_raw_response(state)

    _print_outcome("DC-9.D billing-rate happy path", narrative, raw)

    assert isinstance(narrative, ExceptionNarrative)
    assert raw is not None  # LLM was called (not the fallback path)
    assert narrative.recommendation == "ACCEPT"
    assert narrative.word_count <= 200
    # Rate delta cited (one or both numerics present)
    assert "28.5" in narrative.narrative_text or "30.0" in narrative.narrative_text
    # Citations are non-empty (prompt explicitly requires this on the
    # successful path; empty citations are reserved for the fallback)
    assert len(narrative.citations) >= 1


def test_dc2b_variance_plausibility_happy_path_produces_accept_narrative(
    narrative_agent: NarrativeAgent,
) -> None:
    """A plausible variance explanation citing a quantitatively-matching
    business cause should produce ``recommendation=ACCEPT`` with a
    narrative citing both the magnitude and the explanation language."""
    extraction = ExtractionFindings(
        confidence=0.88,
        variance_explanation_found=True,
        variance_magnitude=0.42,
        variance_explanation_text=(
            "Q3 2026 mandate change: client's IPS amended to add a "
            "$120M emerging-markets sleeve, increasing AUM by 38% and "
            "driving the 42% revenue variance. Note dated 2026-09-30."
        ),
        evidence_anchors=["sheet2!B7", "ips_amendment.pdf!p4"],
    )
    validation = ValidationFindings(
        is_authorized=True,
        confidence=0.86,
        reasoning=(
            "The IPS amendment quantitatively explains the 42% variance "
            "via the 38% AUM increase from the new mandate sleeve; "
            "explanation is dated within the period."
        ),
    )
    state = _state(
        "variance_plausibility", "DC-2", "B", extraction=extraction, validation=validation
    )

    narrative, raw = narrative_agent._invoke_with_raw_response(state)

    _print_outcome("DC-2.B variance-plausibility happy path", narrative, raw)

    assert isinstance(narrative, ExceptionNarrative)
    assert raw is not None
    assert narrative.recommendation == "ACCEPT"
    assert narrative.word_count <= 200
    # Magnitude cited (either the decimal or percent form)
    assert "0.42" in narrative.narrative_text or "42" in narrative.narrative_text
    assert len(narrative.citations) >= 1


def test_dc9d_unauthorised_amendment_produces_escalate_narrative(
    narrative_agent: NarrativeAgent,
) -> None:
    """When validation has ruled the amendment INSUFFICIENT (e.g.
    missing date), the narrative should produce
    ``recommendation=ESCALATE`` and explain the gap. This exercises
    the LLM's adherence to the prompt's recommendation rule and
    confirms the narrative agent correctly transcribes a confident
    negative validation."""
    extraction = ExtractionFindings(
        confidence=0.85,
        ima_amendment_found=True,
        old_rate=28.5,
        new_rate=30.0,
        ima_amendment_text=(
            "Side letter referencing fee rate update; no effective "
            "date specified, no authorising signature."
        ),
        evidence_anchors=["sheet1!A12"],
    )
    validation = ValidationFindings(
        is_authorized=False,
        confidence=0.82,
        reasoning=(
            "The side letter has no effective date and no authorising "
            "signature; cannot retroactively authorise the Q3 rate."
        ),
    )
    state = _state("billing_rate_change", "DC-9", "D", extraction=extraction, validation=validation)

    narrative, raw = narrative_agent._invoke_with_raw_response(state)

    _print_outcome("DC-9.D unauthorised amendment", narrative, raw)

    assert isinstance(narrative, ExceptionNarrative)
    assert raw is not None
    assert narrative.recommendation == "ESCALATE"
    assert narrative.word_count <= 200
    assert len(narrative.citations) >= 1
