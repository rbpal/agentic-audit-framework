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

import pytest

from agentic_audit.layer2_narrative.generator import NarrativeGenerator

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
