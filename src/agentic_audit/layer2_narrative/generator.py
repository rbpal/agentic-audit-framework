"""Layer 2 narrative generator — Azure OpenAI client + chat completion.

This module implements step_05_task_03 in
``privateDocs/step_05_layer2_narrative.md``. The shape:

- ``NarrativeGenerator.generate(attribute, evidence) -> AttributeNarrative``
  is the production entry point. It enforces the runtime guard against
  non-narratable attributes, renders the v1.0 prompt template against
  the evidence, calls Azure OpenAI in JSON mode, parses + validates the
  response, applies the 150-word retry primitive, and assembles the
  ``AttributeNarrative`` with all generation metadata.
- ``NarrativeGenerator._smoke_chat(...)`` is the deployment-liveness
  helper used by the integration test. Not part of the production
  ``generate(...)`` flow — kept around as a deliberate first-commit
  artefact (see git history).

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
- **Prompt rendering uses ``string.Template``**, not ``str.format`` —
  prompts contain literal ``{`` ``}`` for JSON-output schema examples,
  which would conflict with format-string semantics. Same call site
  pattern test_prompt_loader.py has been using since task_01.
- **Word-limit retry is one re-roll, then truncate.** Cap the
  blast radius: at most two LLM calls per ``generate`` (cost ~$0.01),
  and a deterministic fallback if both overrun. The truncated path
  emits a WARN log so it shows up in the eval baseline.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import UTC, datetime
from string import Template
from typing import TYPE_CHECKING, Any

from agentic_audit.layer2_narrative.prompt_loader import load_prompt
from agentic_audit.models.evidence import (
    LAYER3_ATTRIBUTES_PER_CONTROL,
    NARRATABLE_ATTRIBUTES_PER_CONTROL,
    AttributeId,
    ExtractedEvidence,
)
from agentic_audit.models.narrative import (
    AttributeNarrative,
    NarrativeRequest,
    NarrativeResponse,
)
from agentic_audit.models.telemetry import CallUsage, UsageRecorder
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from openai import AzureOpenAI

logger = logging.getLogger(__name__)


# Word-limit constraint per Decision 6.1 in the task doc + the prompt
# template's CONSTRAINTS section. Keep these in sync.
WORD_LIMIT = 150

# Max output tokens for the chat completion. The first task_07 sweep
# (2026-05-06) revealed 300 was too tight: 5 of 32 combinations hit
# JSONDecodeError ("Unterminated string") because the model's response
# was cut off mid-narrative before the closing JSON brace. The narrative
# itself averages 150 words ≈ 200 tokens, but the JSON envelope plus a
# multi-element ``cited_fields`` array can push the full response above
# 300. 500 gives ~250 tokens of headroom — still well below the
# response-size budget but enough to absorb realistic JSON-mode output
# variability. Adjust again if a future sweep surfaces fresh truncation.
_MAX_TOKENS = 500


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
    """Layer 2 narrative generator (task_03).

    Holds the Azure OpenAI client + the deployment / prompt-version
    pinning, and exposes one production entry point —
    ``generate(attribute, evidence) -> AttributeNarrative`` — plus a
    ``_smoke_chat`` helper that the integration test uses to verify the
    deployment is alive end-to-end.

    Pinning the prompt version at init time means every narrative
    produced by THIS instance carries the same version. To run an A/B
    comparison, build two generators with different ``prompt_version``
    arguments — the eval baseline can then group rows in
    ``audit_dev.gold.narratives`` by ``prompt_version`` cleanly.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        prompt_version: str,
        client: AzureOpenAI | None = None,
        api_version: str = AZURE_OPENAI_API_VERSION,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        """Build a generator pinned to a specific deployment + prompt version.

        ``client`` is dependency-injected for tests; in production wiring
        it is None and the constructor builds one via
        ``_build_azure_openai_client``. Pinning the prompt version at
        init time means every narrative produced by this instance carries
        the same version — eval-vs-prompt correlation stays clean.

        ``usage_recorder`` (optional) accumulates per-LLM-call token
        spend across every ``generate(...)`` invocation on this
        instance. Hand the same recorder to one ``NarrativeGenerator``
        per sweep; read its ``snapshot()`` at sweep end to build the
        cost-telemetry row. Default ``None`` skips recording entirely
        (no overhead, no test-mock burden) — only the sweep driver
        opts in.
        """
        self._endpoint = endpoint
        self._deployment = deployment
        self._prompt_version = prompt_version
        self._client = (
            client
            if client is not None
            else _build_azure_openai_client(endpoint=endpoint, api_version=api_version)
        )
        self._usage_recorder = usage_recorder

    @classmethod
    def from_env(
        cls,
        *,
        deployment: str = "gpt-4o",
        prompt_version: str = "v1.0",
        endpoint_env_var: str = "AZURE_OPENAI_ENDPOINT",
        usage_recorder: UsageRecorder | None = None,
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
                "https://aoai-aaf-rbpal-dev.openai.azure.com/) or pass "
                "endpoint explicitly to NarrativeGenerator(...). "
                "Note: the OpenAI subdomain ('aoai-aaf-rbpal-dev') is "
                "configured by Terraform's custom_subdomain_name and "
                "differs from the Cognitive Services account name "
                "('aoai-aaf-dev') — use the subdomain URL here."
            )
        return cls(
            endpoint=endpoint,
            deployment=deployment,
            prompt_version=prompt_version,
            usage_recorder=usage_recorder,
        )

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

    # ---- Production generate flow ----------------------------------

    @traced_function("layer2.narrative_generator.generate")
    def generate(
        self,
        attribute: AttributeId,
        evidence: ExtractedEvidence,
        *,
        generation_run_id: str | None = None,
    ) -> AttributeNarrative:
        """Generate one grounded narrative for ``(attribute, evidence)``.

        Steps:

        1. Runtime guard — reject if ``attribute`` is reserved for
           Layer 3 per ``NARRATABLE_ATTRIBUTES_PER_CONTROL``. Loud
           ``ValueError`` with a helpful message.
        2. Build ``NarrativeRequest`` — JSON-serialise the relevant
           per-attribute slice of ``evidence``.
        3. Render the v1.0 prompt template against the request.
        4. Call Azure OpenAI in JSON mode (temperature 0, max 300
           tokens). Parse the response into ``NarrativeResponse``.
        5. If ``word_count`` > 150, retry once with a stricter prompt;
           on second overrun, truncate + log WARN.
        6. Assemble + return the ``AttributeNarrative`` with all
           generation metadata. ``fact_check_passed`` defaults False —
           the fact-checker (task_05) flips it after verification.

        ``generation_run_id`` ties this narrative to a specific sweep
        invocation; if None, a fresh ULID-style id is minted. Passing
        the same id across N ``generate`` calls produces a coherent
        cohort (one row in ``audit_dev.gold.cost_telemetry`` summarises
        the whole sweep).

        If a ``UsageRecorder`` was passed at construction time, every
        billed LLM call inside this method (1 or 2 calls — the
        word-limit retry can fire) is recorded on it. The sweep driver
        reads the recorder's snapshot at sweep end to produce the
        cost-telemetry row.
        """
        narratable = NARRATABLE_ATTRIBUTES_PER_CONTROL[evidence.control_id]
        if attribute not in narratable:
            reserved = LAYER3_ATTRIBUTES_PER_CONTROL[evidence.control_id]
            raise ValueError(
                f"attribute {attribute!r} is not narratable for "
                f"{evidence.control_id!r}; narratable: {tuple(narratable)}. "
                f"Reserved for Layer 3: {tuple(reserved)}. See Decision 2 in "
                "privateDocs/step_05_layer2_narrative.md."
            )

        request = NarrativeRequest(
            control_id=evidence.control_id,
            attribute_id=attribute,
            quarter=evidence.quarter,
            evidence_json=self._build_evidence_json(attribute, evidence),
        )
        prompt = self._render_prompt(request)
        response = self._call_with_word_limit_retry(prompt)

        return AttributeNarrative(
            engagement_id=evidence.engagement_id,
            control_id=evidence.control_id,
            attribute_id=attribute,
            quarter=evidence.quarter,
            source_evidence_id=self._source_evidence_id(evidence, attribute),
            narrative_text=response.narrative_text,
            cited_fields=response.cited_fields,
            word_count=response.word_count,
            prompt_version=self._prompt_version,
            model_deployment=self._deployment,
            generation_run_id=generation_run_id or self._new_run_id(),
            generated_at=datetime.now(UTC),
        )

    # ---- Helpers ---------------------------------------------------

    @staticmethod
    def _build_evidence_json(attribute: AttributeId, evidence: ExtractedEvidence) -> str:
        """Build the evidence JSON payload injected into the prompt.

        Includes envelope (engagement, control, quarter, preparer,
        reviewer, source lineage) plus the single ``AttributeCheck``
        being narrated. Including the full attribute set would burn
        prompt tokens on attributes the LLM is told to ignore — keep
        the payload focused.
        """
        target = next(
            (a for a in evidence.attributes if a.attribute_id == attribute),
            None,
        )
        if target is None:
            # The runtime guard above + ExtractedEvidence's own
            # validator should make this unreachable; raising is a
            # belt-and-braces guard for hand-constructed test payloads.
            raise ValueError(
                f"attribute {attribute!r} not present in evidence for "
                f"({evidence.engagement_id!r}, {evidence.control_id!r}, "
                f"{evidence.quarter!r})"
            )
        payload = {
            "engagement_id": evidence.engagement_id,
            "control_id": evidence.control_id,
            "attribute_id": attribute,
            "quarter": evidence.quarter,
            "extraction_timestamp": evidence.extraction_timestamp.isoformat(),
            "preparer": {
                "initials": evidence.preparer.initials,
                "role": evidence.preparer.role,
                "date": evidence.preparer.date.isoformat(),
            },
            "reviewer": {
                "initials": evidence.reviewer.initials,
                "role": evidence.reviewer.role,
                "date": evidence.reviewer.date.isoformat(),
            },
            "source_path": evidence.source_path,
            "source_file_hash": evidence.source_bronze_file_hash,
            "attribute_check": {
                "status": target.status,
                "evidence_cell_refs": target.evidence_cell_refs,
                "extracted_value": target.extracted_value,
                "notes": target.notes,
            },
        }
        return json.dumps(payload, sort_keys=True, default=str)

    def _render_prompt(self, request: NarrativeRequest) -> str:
        """Substitute ``${}`` placeholders in the v1.0 prompt template
        with the request's fields. Raises ``KeyError`` if the template
        has placeholders the request can't satisfy — that's a prompt
        drift bug, fail loudly."""
        template_text = load_prompt(self._prompt_version)
        return Template(template_text).substitute(
            control_id=request.control_id,
            attribute_id=request.attribute_id,
            quarter=request.quarter,
            evidence_json=request.evidence_json,
        )

    def _call_with_word_limit_retry(self, prompt: str) -> NarrativeResponse:
        """Call the LLM. If response exceeds ``WORD_LIMIT``, retry once
        with a stricter suffix. On second overrun, truncate + WARN."""
        first = self._invoke_llm(prompt)
        if first.word_count <= WORD_LIMIT:
            return first

        logger.info(
            "narrative exceeded %d words on first attempt (%d words); retrying",
            WORD_LIMIT,
            first.word_count,
        )
        stricter = (
            f"{prompt}\n\n"
            f"PREVIOUS RESPONSE WAS {first.word_count} WORDS, OVER THE "
            f"{WORD_LIMIT}-WORD LIMIT. PRODUCE A SHORTER NARRATIVE STRICTLY "
            f"UNDER {WORD_LIMIT} WORDS. SAME JSON SCHEMA."
        )
        second = self._invoke_llm(stricter)
        if second.word_count <= WORD_LIMIT:
            return second

        logger.warning(
            "narrative exceeded %d words on both attempts (%d, %d); truncating",
            WORD_LIMIT,
            first.word_count,
            second.word_count,
        )
        truncated_words = second.narrative_text.split()[:WORD_LIMIT]
        return NarrativeResponse(
            narrative_text=" ".join(truncated_words),
            cited_fields=second.cited_fields,
            word_count=len(truncated_words),
        )

    def _invoke_llm(self, prompt: str) -> NarrativeResponse:
        """Single chat completion in JSON mode → parsed
        ``NarrativeResponse``. Raises if the response is empty or the
        JSON doesn't match the schema. The pydantic validation is
        load-bearing — if GPT-4o returns extra/missing fields,
        ``NarrativeResponse(**payload)`` raises and the caller can
        decide whether to retry.

        On ``json.JSONDecodeError``, retries the call once before
        raising. The first task_07 sweep showed gpt-4o occasionally
        emits malformed JSON despite ``response_format={"type":
        "json_object"}`` (subtype: ``"Expecting property name enclosed
        in double quotes"``). A single retry with the same prompt
        usually recovers because the failure mode is non-deterministic
        even at ``temperature=0``. The truncation subtype
        (``"Unterminated string"``) was the dominant pre-task_08
        failure and is addressed by the larger ``_MAX_TOKENS``.
        """
        for attempt in (1, 2):
            response = self._client.chat.completions.create(
                model=self._deployment,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=_MAX_TOKENS,
                response_format={"type": "json_object"},
            )
            self._record_usage_if_present(response)
            content = response.choices[0].message.content
            if not content:
                raise ValueError(
                    f"empty response from {self._deployment!r}; "
                    f"finish_reason="
                    f"{response.choices[0].finish_reason!r}"
                )
            try:
                payload = json.loads(content)
            except json.JSONDecodeError as exc:
                if attempt == 1:
                    logger.warning(
                        "malformed JSON from %s on attempt 1 (%s); retrying once",
                        self._deployment,
                        exc.msg,
                    )
                    continue
                # Second attempt also failed — raise with the raw
                # content embedded so the caller can debug without
                # re-running the LLM.
                raise ValueError(
                    f"malformed JSON from {self._deployment!r} on both "
                    f"attempts: {exc.msg}; raw content[:500]="
                    f"{content[:500]!r}"
                ) from exc
            return NarrativeResponse(**payload)
        # Unreachable — the loop either returns or raises. Pylint-pacifier.
        raise RuntimeError("unreachable: _invoke_llm exhausted both attempts without returning")

    def _record_usage_if_present(self, response: Any) -> None:
        """Record per-call token usage on the recorder if one is wired.

        Recording happens after every billed API call, regardless of
        whether the response body parses successfully — token usage is
        what the cloud meters, and a malformed-JSON response was still
        a billed call. Skipped silently when no recorder is wired (the
        default for unit tests, integration tests, and ad-hoc REPL use).

        ``response.usage`` may be ``None`` for some streaming-mode
        responses, but JSON-mode chat completions always populate it.
        Defensive against a missing field anyway: any AttributeError
        from accessing the OpenAI SDK shape silently degrades to "no
        record" rather than crashing the sweep.
        """
        if self._usage_recorder is None:
            return
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if prompt_tokens is None or completion_tokens is None:
            return
        self._usage_recorder.record(
            CallUsage(
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
            )
        )

    @staticmethod
    def _source_evidence_id(evidence: ExtractedEvidence, attribute: AttributeId) -> str:
        """Stable id for the silver row this narrative is derived from.

        Natural-key form (engagement|control|attribute|quarter) — same
        grain as silver.evidence's MERGE key. Human-readable; survives
        re-extraction; doesn't depend on knowing silver_writer's bigint
        hash.
        """
        return f"{evidence.engagement_id}|{evidence.control_id}|{attribute}|{evidence.quarter}"

    @staticmethod
    def _new_run_id() -> str:
        """Mint a fresh per-call run id when the caller doesn't supply
        one. Random hex (not a real ULID — just a unique string) so
        ad-hoc generate() calls don't collide; sweep scripts pass a
        shared id to group a cohort."""
        return secrets.token_hex(16).upper()


__all__ = [
    "AZURE_OPENAI_API_VERSION",
    "WORD_LIMIT",
    "NarrativeGenerator",
]
