"""End-to-end integration test for Layer 2 narrative generator
(step_05_task_03 first commit).

Marked ``@pytest.mark.slow`` and gated on the
``AZURE_OPENAI_ENDPOINT`` env var. CI's default unit-test pass skips
this. Run on demand with::

    AZURE_OPENAI_ENDPOINT=https://aoai-aaf-dev.openai.azure.com/  \\
    poetry run pytest -m slow tests/integration/test_layer2_generator_e2e.py -v

Authentication: ``DefaultAzureCredential`` resolves the Azure auth
chain. Locally that's a cached ``az login`` token (the same one
Terraform's been using). On Databricks compute it would be the job
MSI. No PAT / API key required.

What this test verifies:

1. ``DefaultAzureCredential`` resolves a valid Cognitive Services
   bearer token (auth chain works).
2. The Azure OpenAI account at ``AZURE_OPENAI_ENDPOINT`` is reachable.
3. The configured deployment (default ``gpt-4o``) exists and responds.
4. A trivial chat completion ("Reply with exactly the word OK")
   returns ``"OK"`` (or contains it).

This is the verification we deferred from task_02 closeout — see
``privateDocs/step_05_layer2_narrative.md`` >
``step_05_task_03_generator`` > "Critical first action". If this test
fails, the rest of task_03 should NOT proceed until the deployment
is fixed.

Cost per run: ~$0.00005 (5 input tokens + ~3 output tokens at
gpt-4o pricing). Idempotent — safe to re-run repeatedly.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from agentic_audit.layer2_narrative.generator import WORD_LIMIT, NarrativeGenerator
from agentic_audit.models.evidence import (
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)
from agentic_audit.models.narrative import AttributeNarrative

pytestmark = pytest.mark.slow


def _have_azure_openai_endpoint() -> bool:
    return bool(os.getenv("AZURE_OPENAI_ENDPOINT"))


@pytest.fixture(scope="module")
def generator() -> NarrativeGenerator:
    """Build a NarrativeGenerator from env vars, skipping if the
    endpoint isn't configured.

    The DefaultAzureCredential chain is exercised inside ``from_env``;
    if ``az login`` is stale the smoke chat will fail later with a
    clear AAD error pointing at the remediation.
    """
    if not _have_azure_openai_endpoint():
        pytest.skip(
            "AZURE_OPENAI_ENDPOINT not set; skipping live integration test. "
            "Export e.g. AZURE_OPENAI_ENDPOINT=https://aoai-aaf-dev.openai.azure.com/ "
            "and ensure `az login` is active."
        )
    return NarrativeGenerator.from_env()


def test_smoke_chat_returns_ok(generator: NarrativeGenerator) -> None:
    """Trivial chat completion: send 'Reply with exactly the word OK',
    expect 'OK' in the response.

    This is the deployment liveness check. It exercises the same auth
    chain + client construction + chat completion API path that the
    full ``generate(...)`` method (subsequent commit) will use.
    """
    response = generator._smoke_chat()

    assert len(response.choices) >= 1
    content = response.choices[0].message.content
    assert content is not None
    assert "OK" in content.upper(), (
        f"Expected 'OK' in response content; got {content!r}. "
        "Either the deployment is throttled, returning unexpected text, "
        "or the model is misconfigured."
    )


def test_smoke_chat_token_usage_is_reported(generator: NarrativeGenerator) -> None:
    """The Azure OpenAI response carries token usage. We rely on this
    in the full generator (subsequent commit) to populate cost
    telemetry. Verifying the field is present here means task_07's
    cost telemetry write will have data to write."""
    response = generator._smoke_chat()

    assert response.usage is not None
    assert response.usage.prompt_tokens > 0
    assert response.usage.completion_tokens > 0
    assert response.usage.total_tokens == (
        response.usage.prompt_tokens + response.usage.completion_tokens
    )


def test_deployment_pinning_matches_env(generator: NarrativeGenerator) -> None:
    """Generator's deployment property reflects what was requested.
    Defaults from ``from_env`` are sane for the dev environment."""
    assert generator.deployment == "gpt-4o"
    assert generator.prompt_version == "v1.0"


# ---- Full generate() flow (the production path) ------------------------

UTC_TS = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)


def _synthetic_dc9_q1_evidence() -> ExtractedEvidence:
    """Build a deterministic ExtractedEvidence for the live generate()
    test. Uses DC-9.A (preparer sign-off) — narratable for DC-9, simple
    enough that the LLM should produce a clean grounded narrative."""
    attrs = [
        AttributeCheck(
            control_id="DC-9",
            attribute_id="A",
            status="pass",
            evidence_cell_refs=["DC-9 Billing!r4c1"],
            extracted_value="AB — 2026-01-15",
            notes="preparer signed",
        ),
    ] + [
        AttributeCheck(
            control_id="DC-9",
            attribute_id=a,  # type: ignore[arg-type]
            status="pass",
            evidence_cell_refs=[f"DC-9 Billing!{a}"],
            extracted_value=None,
            notes=None,
        )
        for a in ("B", "C", "D", "E", "F")
    ]
    return ExtractedEvidence(
        engagement_id="alpha-pension-fund-2025",
        control_id="DC-9",
        quarter="Q1",
        run_id="LIVE-GENERATE-TEST",
        extraction_timestamp=UTC_TS,
        preparer=SignOff(initials="AB", role="preparer", date=UTC_TS),
        reviewer=SignOff(initials="CD", role="reviewer", date=UTC_TS),
        attributes=attrs,
        source_bronze_file_hash="a" * 64,
        source_path="abfss://bronze@dlsaafrbpaldev.dfs.core.windows.net/corpus/v2/workpapers/dc9_Q1_ref.xlsx",
    )


def test_generate_dc9_attribute_a_produces_grounded_narrative(
    generator: NarrativeGenerator,
) -> None:
    """End-to-end: real prompt template + real evidence → live gpt-4o
    in JSON mode → parsed AttributeNarrative.

    This is the test that catches prompt-template / JSON-mode
    integration bugs the mocked unit tests can't see. Specifically
    verifies:

    1. The v1.0 template renders without leftover ``${}`` placeholders
       (the runtime guard would not catch this — only a real
       Template.substitute call does).
    2. JSON mode + this specific prompt actually returns the expected
       NarrativeResponse schema (gpt-4o's actual response, not a mock).
    3. The 150-word constraint is being respected by the LLM (or at
       least, the word-limit retry primitive is keeping it under).
    4. AttributeNarrative is assembled with all metadata.

    Cost: ~$0.005 per run (one or two LLM calls, ~50-200 tokens each).
    """
    evidence = _synthetic_dc9_q1_evidence()

    narrative = generator.generate("A", evidence, generation_run_id="LIVE-TEST-RUN-001")

    # Type + identity
    assert isinstance(narrative, AttributeNarrative)
    assert narrative.engagement_id == "alpha-pension-fund-2025"
    assert narrative.control_id == "DC-9"
    assert narrative.attribute_id == "A"
    assert narrative.quarter == "Q1"
    assert narrative.generation_run_id == "LIVE-TEST-RUN-001"
    assert narrative.prompt_version == "v1.0"
    assert narrative.model_deployment == "gpt-4o"

    # Content sanity
    assert narrative.narrative_text  # non-empty
    assert narrative.word_count > 0
    assert narrative.word_count <= WORD_LIMIT, (
        f"narrative ran {narrative.word_count} words, over the {WORD_LIMIT} limit; "
        "word-limit retry primitive should have caught this"
    )
    # Default fact-check state — fact_checker (task_05) flips it later
    assert narrative.fact_check_passed is False
    assert narrative.fact_check_issues == []


def test_generate_dc9_d_rejected_at_runtime_guard_no_llm_call(
    generator: NarrativeGenerator,
) -> None:
    """Sanity: the runtime guard fires BEFORE any live LLM call when
    a non-narratable attribute is requested. Costs $0 — the test
    exists to prove the guard works against the actual generator
    instance (not just a mocked one)."""
    evidence = _synthetic_dc9_q1_evidence()

    with pytest.raises(ValueError, match="not narratable for 'DC-9'"):
        generator.generate("D", evidence)
