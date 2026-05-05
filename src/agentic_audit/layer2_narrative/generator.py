"""Layer 2 narrative generator — Azure OpenAI client + chat completion.

This module implements step_05_task_03 in
``privateDocs/step_05_layer2_narrative.md``. The current commit is the
**minimum-viable shape** — enough to verify the live ``gpt-4o``
deployment is alive (Option B from the pre-execution notes).

Subsequent commits within the task_03 PR add:

- Prompt rendering (``string.Template`` substitution against
  ``NarrativeRequest``).
- Runtime guard (reject non-narratable attributes per
  ``NARRATABLE_ATTRIBUTES_PER_CONTROL``).
- ``generate(attribute, evidence) -> AttributeNarrative`` proper.
- Word-limit retry primitive (shared with task_04).
- ``@traced_function`` decorator on entry points.
- Full unit test coverage with mocked Azure OpenAI client.

Design choices baked in (per ``privateDocs/step_05_layer2_narrative.md``
Decisions):

- **Azure OpenAI auth via managed identity** (Decision 6.1 + Step 2's
  MSI setup). ``DefaultAzureCredential`` resolves the auth chain —
  locally that's a cached ``az login`` token, on Databricks that's the
  job MSI. Same code path either way. NEVER use API-key auth — Step 2
  deliberately did not provision a Key Vault secret rotation story.
- **Lazy import** of ``azure-identity`` inside the factory. Mirrors
  the ``BronzeReader`` / ``SilverEvidenceReader`` pattern: unit tests
  can mock the factory without the auth library installed.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openai import AzureOpenAI


# Pinned API version. Update when the project decides to bump.
# 2024-10-21 supports response_format={"type": "json_object"} which
# task_03's full generator will rely on.
AZURE_OPENAI_API_VERSION = "2024-10-21"

# Cognitive Services scope for the bearer token. Same value across all
# Azure OpenAI deployments; documented at
# https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/managed-identity
_COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"


def _build_azure_openai_client(
    *, endpoint: str, api_version: str = AZURE_OPENAI_API_VERSION
) -> AzureOpenAI:
    """Build an ``AzureOpenAI`` client backed by ``DefaultAzureCredential``.

    Lazy-imports both ``azure.identity`` and ``openai`` so unit tests
    that mock the client never need the libraries installed.

    Resolution chain (in order, first match wins):

    1. Environment variables (``AZURE_CLIENT_ID`` etc.) — explicit SP.
    2. Workload identity (Kubernetes / Databricks job MSI).
    3. Managed identity (system-assigned MSI on the compute).
    4. Shared token cache (Visual Studio, IntelliJ).
    5. Azure CLI (``az login`` cached token) — local dev path.

    Local dev: just ``az login`` with the right subscription. CI: workload
    identity or job MSI. No API key, no Key Vault, no rotation.
    """
    from azure.identity import (  # noqa: PLC0415  (lazy import — see module docstring)
        DefaultAzureCredential,
        get_bearer_token_provider,
    )
    from openai import AzureOpenAI  # noqa: PLC0415

    token_provider = get_bearer_token_provider(DefaultAzureCredential(), _COGNITIVE_SERVICES_SCOPE)
    return AzureOpenAI(
        azure_endpoint=endpoint,
        api_version=api_version,
        azure_ad_token_provider=token_provider,
    )


class NarrativeGenerator:
    """Layer 2 narrative generator (minimal — task_03 first commit).

    Holds the Azure OpenAI client and the deployment / prompt-version
    pinning. The ``generate(attribute, evidence)`` method that produces
    ``AttributeNarrative`` is intentionally NOT implemented yet — that
    arrives in subsequent commits within the task_03 PR. This first
    commit only provides the construction path + a smoke chat method
    so the live deployment can be verified end-to-end.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        prompt_version: str,
        client: AzureOpenAI | None = None,
        api_version: str = AZURE_OPENAI_API_VERSION,
    ) -> None:
        """Build a generator pinned to a specific deployment + prompt version.

        ``client`` is dependency-injected for tests; in production wiring
        it is None and the constructor builds one via
        ``_build_azure_openai_client``. Pinning the prompt version at
        init time means every narrative produced by this instance carries
        the same version — eval-vs-prompt correlation stays clean.
        """
        self._endpoint = endpoint
        self._deployment = deployment
        self._prompt_version = prompt_version
        self._client = (
            client
            if client is not None
            else _build_azure_openai_client(endpoint=endpoint, api_version=api_version)
        )

    @classmethod
    def from_env(
        cls,
        *,
        deployment: str = "gpt-4o",
        prompt_version: str = "v1.0",
        endpoint_env_var: str = "AZURE_OPENAI_ENDPOINT",
    ) -> NarrativeGenerator:
        """Build from environment variable for the endpoint.

        Production wiring: set ``AZURE_OPENAI_ENDPOINT`` (and authenticate
        via ``az login`` locally or job MSI on Databricks). Defaults are
        sane for the dev environment per Decision 6.1.
        """
        endpoint = os.environ.get(endpoint_env_var)
        if not endpoint:
            raise RuntimeError(
                f"environment variable {endpoint_env_var!r} is not set; "
                "either export it (e.g. "
                "https://aoai-aaf-dev.openai.azure.com/) or pass endpoint "
                "explicitly to NarrativeGenerator(...)."
            )
        return cls(endpoint=endpoint, deployment=deployment, prompt_version=prompt_version)

    @property
    def deployment(self) -> str:
        return self._deployment

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def _smoke_chat(self, user_message: str = "Reply with exactly the word OK.") -> Any:
        """One-shot chat completion for liveness verification.

        Used by the integration test in
        ``tests/integration/test_layer2_generator_e2e.py`` to confirm
        that the configured Azure OpenAI deployment is alive and that
        the auth chain resolves correctly. Not part of the production
        ``generate(...)`` flow — intentionally a separate method so it
        can be exercised independently, including in a one-off
        verification script.

        Returns the raw OpenAI ``ChatCompletion`` response. Caller is
        expected to assert on ``response.choices[0].message.content``.
        """
        return self._client.chat.completions.create(
            model=self._deployment,  # Azure: this is the DEPLOYMENT name, not the model id
            messages=[{"role": "user", "content": user_message}],
            temperature=0,
            max_tokens=8,
        )


__all__ = [
    "AZURE_OPENAI_API_VERSION",
    "NarrativeGenerator",
]
