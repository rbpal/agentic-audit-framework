"""Layer 3 Validation sub-agent (Step 7 task_05).

Single LLM call (NOT a ReAct agent — no tools needed) that judges
whether the supporting document the Extraction sub-agent surfaced is
sufficient to authorise the flagged exception. Two prompt variants per
``exception_type`` share one JSON output schema (``ValidationFindings``).

Design choices (mirrors Layer 2's ``Judge``, NOT the Extraction agent):

- **Raw ``openai.AzureOpenAI`` client + JSON mode**, not LangChain's
  ``AzureChatOpenAI``. Validation needs one chat-completion call with a
  structured-JSON response — ``create_react_agent`` would be dead weight.
  Reuses ``layer2_narrative.generator._build_azure_openai_client`` as
  the construction path, same managed-identity auth chain as Layer 2's
  generator + judge.
- **Fast path on missing document.** When the Extraction sub-agent
  reports ``ima_amendment_found=False`` (billing) or
  ``variance_explanation_found=False`` (variance), the validation
  prompt is **skipped entirely** and a deterministic
  ``ValidationFindings(is_authorized=False, confidence=0.9,
  reasoning="No supporting document found")`` returned. Cheap and
  confident on the negative — and one of the most common Layer-3 paths
  once the Step 8 tools are wired against real evidence.
- **Single retry on parse / validation failure**, then a deterministic
  ``is_authorized=False, confidence=0.0`` fallback. Same posture as
  the Layer-2 ``Judge`` — Validation must never raise into the
  supervisor; a malformed response surfaces as a fail-closed verdict
  with a diagnostic ``reasoning`` string.
- **Prompt-version pinning.** Each ``ValidationAgent`` holds one
  ``prompt_version`` (e.g. ``"v1.0"``) and resolves the per-exception-
  type variant by appending the type to the filename. Same convention
  as the Extraction agent — a future v1.1 cycle bumps once and both
  variants flip atomically.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Any

from ai_ops_kit import traced_llm_call
from pydantic import ValidationError

from agentic_audit.layer2_narrative.generator import (
    AZURE_OPENAI_API_VERSION,
    _build_azure_openai_client,
)
from agentic_audit.layer3_agents.state import (
    ExceptionType,
    ExtractionFindings,
    InvestigationState,
    ValidationFindings,
)
from agentic_audit.models.telemetry import CallUsage, UsageRecorder, estimate_cost_usd
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from openai import AzureOpenAI

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Smaller envelope than Judge / NarrativeGenerator — three short fields
# (is_authorized + confidence + reasoning). 400 tokens leaves comfortable
# headroom for a paragraph of reasoning without a truncation failure mode.
_MAX_TOKENS = 400

# Confidence floor on the no-document fast path. High because the
# negative is almost always correct: Extraction surfaced no document =>
# Validation cannot find authorisation. The 0.1 reserved gap accounts
# for the rare case where the Extraction agent misses a document the
# tooling could in principle have read — narrows further once Step 8
# instruments tool-call recall.
_FAST_PATH_CONFIDENCE: float = 0.9

# Reason string returned with the fast-path verdict. Stable so
# downstream consumers (the narrative agent, the gold table) can grep
# for the no-document case without LLM-text variance.
_FAST_PATH_REASON: str = "No supporting document found"


class ValidationAgent:
    """Single-call LLM judge over IMA-amendment / variance-explanation
    sufficiency.

    Pinned to a deployment + prompt version at construction. Renders
    the per-``exception_type`` prompt against the live state +
    extraction findings, calls Azure OpenAI in JSON mode, parses the
    response into ``ValidationFindings``, and returns. Retries once on
    parse / validation failure; on a second failure returns a
    deterministic fail-closed verdict.

    The fast-path check (no document found in extraction) short-circuits
    BEFORE the LLM call — same instance can serve thousands of
    investigations where most fire the cheap path.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str = "gpt-4o",
        prompt_version: str = "v1.0",
        client: AzureOpenAI | None = None,
        api_version: str = AZURE_OPENAI_API_VERSION,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        """Build a Validation sub-agent.

        ``client`` is dependency-injected for tests; in production it is
        ``None`` and the constructor builds one via
        ``_build_azure_openai_client`` (same factory the Layer 2
        generator + judge use). ``prompt_version`` is the suffix that
        resolves to the per-exception-type prompt files — ``"v1.0"``
        reads ``validation_v1_0_billing_rate_change.txt`` and
        ``validation_v1_0_variance_plausibility.txt``.

        ``usage_recorder`` is optional; when supplied, every billed
        ``chat.completions.create`` call records its token usage on
        the recorder. The supervisor's ``run_investigation`` constructs
        a fresh recorder per call and passes it through; the cost-
        telemetry row is built from the recorder's snapshot at exit.
        Fast-path validations (no LLM call) leave the recorder
        untouched.
        """
        self._endpoint = endpoint
        self._deployment = deployment
        self._prompt_version = prompt_version
        self._api_version = api_version
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
    ) -> ValidationAgent:
        """Build from ``AZURE_OPENAI_ENDPOINT``. Same env contract as
        ``Judge.from_env`` and ``ExtractionAgent.from_env``."""
        endpoint = os.environ.get(endpoint_env_var)
        if not endpoint:
            raise RuntimeError(
                f"environment variable {endpoint_env_var!r} is not set; "
                "either export it (e.g. "
                "https://aoai-aaf-rbpal-dev.openai.azure.com/) or pass "
                "endpoint explicitly to ValidationAgent(...)."
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

    # ── Production entry point ──────────────────────────────────────

    @traced_function("layer3.validation_agent.invoke")
    def invoke(self, state: InvestigationState) -> ValidationFindings:
        """Run one validation pass against the live investigation state.

        Pre-condition: ``state["extraction_findings"]`` is populated
        (the supervisor routes to validation only after extraction
        lands). A missing extraction surfaces as ``ValueError`` rather
        than a silent wrong-prompt path.

        Side-effect-free for the fast path; one billed Azure OpenAI
        call for the LLM path.
        """
        findings, _raw = self._invoke_with_raw_response(state)
        return findings

    def _invoke_with_raw_response(
        self, state: InvestigationState
    ) -> tuple[ValidationFindings, str | None]:
        """Diagnostic variant of ``invoke`` — returns findings PLUS the
        raw LLM JSON content (or ``None`` for the fast path).

        Production callers should use ``invoke`` which discards the
        raw content. The slow integration test prints the raw content
        for operator review (mirrors the Extraction agent's
        ``_invoke_with_messages`` helper).
        """
        extraction = state.get("extraction_findings")
        if extraction is None:
            raise ValueError(
                "ValidationAgent.invoke: state['extraction_findings'] is None. "
                "The supervisor must route to extraction first; calling "
                "validation on an empty extraction is a routing bug."
            )

        exception_type = state["exception_type"]

        # Fast path — no document found in extraction. Skip the LLM
        # call entirely. This is the common Layer-3 outcome once Step 8
        # tools are wired against engagements where the auditor flagged
        # an exception precisely because the supporting document is
        # missing.
        if self._is_no_document_case(extraction, exception_type):
            return (
                ValidationFindings(
                    is_authorized=False,
                    confidence=_FAST_PATH_CONFIDENCE,
                    reasoning=_FAST_PATH_REASON,
                ),
                None,
            )

        prompt = self._render_prompt(state, extraction, exception_type)
        return self._invoke_with_fallback(prompt)

    # ── Fast-path detection ─────────────────────────────────────────

    @staticmethod
    def _is_no_document_case(
        extraction: ExtractionFindings,
        exception_type: ExceptionType,
    ) -> bool:
        """True iff the extraction sub-agent reports no supporting
        document for the relevant exception type.

        Branches on ``exception_type`` because ``ExtractionFindings``
        carries both subsets on one model — checking the wrong
        ``found`` flag would silently fire the fast path on the wrong
        exception.
        """
        if exception_type == "billing_rate_change":
            return extraction.ima_amendment_found is False
        # variance_plausibility — only other branch the Literal admits.
        return extraction.variance_explanation_found is False

    # ── Prompt rendering ────────────────────────────────────────────

    def _render_prompt(
        self,
        state: InvestigationState,
        extraction: ExtractionFindings,
        exception_type: ExceptionType,
    ) -> str:
        """Render the per-exception-type validation prompt template.

        The two variants substitute different fields off the
        ``ExtractionFindings`` (billing → old/new rate + amendment text;
        variance → magnitude + explanation text). ``""`` is substituted
        for any optional field that came back ``None`` from extraction —
        the prompt copy handles the empty-text case gracefully and the
        fast path catches the truly-missing case before we get here.
        """
        filename = f"validation_{self._prompt_version.replace('.', '_')}_{exception_type}.txt"
        template_text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")

        if exception_type == "billing_rate_change":
            return Template(template_text).substitute(
                engagement_id=state["engagement_id"],
                control_id=state["control_id"],
                attribute_id=state["attribute_id"],
                quarter=state["quarter"],
                old_rate=_fmt_optional(extraction.old_rate),
                new_rate=_fmt_optional(extraction.new_rate),
                ima_amendment_text=extraction.ima_amendment_text or "",
            )
        # variance_plausibility
        return Template(template_text).substitute(
            engagement_id=state["engagement_id"],
            control_id=state["control_id"],
            attribute_id=state["attribute_id"],
            quarter=state["quarter"],
            variance_magnitude=_fmt_optional(extraction.variance_magnitude),
            variance_explanation_text=extraction.variance_explanation_text or "",
        )

    # ── LLM invocation + fallback ───────────────────────────────────

    def _invoke_with_fallback(self, prompt: str) -> tuple[ValidationFindings, str | None]:
        """Single retry on LLM-side failure; deterministic fail-closed
        fallback on second failure.

        Failure modes the inner loop handles (same set as the Layer-2
        ``Judge``):

        - ``content is None`` — empty response (e.g. ``finish_reason ==
          'length'``). Treated as parse failure.
        - ``json.JSONDecodeError`` — JSON mode occasionally emits
          invalid JSON despite the ``response_format`` constraint.
        - ``pydantic.ValidationError`` — JSON parsed but
          ``ValidationFindings`` rejected it (e.g. confidence out of
          range, empty reasoning, extra fields).

        On the second failure of any of the above, log WARN and return
        a fail-closed verdict (``is_authorized=False, confidence=0.0``).
        Never raise — the supervisor must always get back a valid
        ``ValidationFindings``.
        """
        for attempt in (1, 2):
            try:
                response = self._traced_chat_completion(prompt)["response"]
            except Exception as exc:  # pragma: no cover — network/auth path
                if attempt == 1:
                    logger.warning(
                        "ValidationAgent LLM call failed on attempt 1 (%s); retrying", exc
                    )
                    continue
                return self._fallback_to_unauthorized(f"LLM call failed x2: {exc}"), None

            content = response.choices[0].message.content
            if not content:
                if attempt == 1:
                    logger.warning(
                        "ValidationAgent returned empty content on attempt 1 "
                        "(finish_reason=%r); retrying",
                        response.choices[0].finish_reason,
                    )
                    continue
                return (
                    self._fallback_to_unauthorized(
                        f"empty content x2 (finish_reason={response.choices[0].finish_reason!r})"
                    ),
                    None,
                )

            try:
                payload = json.loads(content)
                return ValidationFindings(**payload), content
            except json.JSONDecodeError as exc:
                if attempt == 1:
                    logger.warning(
                        "ValidationAgent JSON parse failed on attempt 1 (%s); retrying", exc.msg
                    )
                    continue
                return (
                    self._fallback_to_unauthorized(f"JSON parse failure x2: {exc.msg}"),
                    content,
                )
            except ValidationError as exc:
                if attempt == 1:
                    logger.warning(
                        "ValidationAgent response validation failed on attempt 1; retrying"
                    )
                    continue
                return (
                    self._fallback_to_unauthorized(f"validation failure x2: {exc.errors()}"),
                    content,
                )

        # Unreachable — both attempts either return or fall through.
        raise RuntimeError("unreachable: _invoke_with_fallback exhausted both attempts")

    @traced_llm_call(model="layer3_validation")
    def _traced_chat_completion(self, prompt: str) -> dict[str, Any]:
        """Single AOAI chat.completions.create call wrapped for OTel.

        The ``@traced_llm_call`` decorator emits an ``llm.layer3_validation``
        span and surfaces ``prompt_tokens / completion_tokens / total_tokens
        / total_cost_usd / model_version`` from the return dict as
        ``llm.*`` span attributes — what Workbook 2 (`Cost & Tokens`)
        reads. The ``response`` key is opaque to the decorator and
        passes through to the caller for content parsing.

        Also invokes ``self._record_usage_if_present`` so the existing
        ``UsageRecorder`` pipeline (``gold.cost_telemetry``) is
        unaffected — the two telemetry pipes are independent.
        """
        response = self._client.chat.completions.create(
            model=self._deployment,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
        self._record_usage_if_present(response)
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        cost = estimate_cost_usd(
            deployment=self._deployment,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
        )
        return {
            "response": response,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "total_cost_usd": cost if cost is not None else 0.0,
            "model_version": self._deployment,
        }

    def _record_usage_if_present(self, response: Any) -> None:
        """Record per-call token usage on the recorder if one is wired.

        Same shape as ``layer2_narrative.Judge._record_usage_if_present``:
        recording fires after every billed API call regardless of
        parse outcome — token usage is what the cloud meters, and a
        malformed-JSON response was still a billed call.
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
    def _fallback_to_unauthorized(reason: str) -> ValidationFindings:
        """Build the deterministic fail-closed fallback. Reasoning text
        carries the diagnostic so operators can grep
        ``gold.layer3_decisions`` for failure modes."""
        return ValidationFindings(
            is_authorized=False,
            confidence=0.0,
            reasoning=f"Validation failure x2; falling back to unauthorized ({reason})",
        )

    # ── Test utility ────────────────────────────────────────────────

    def _peek_rendered_prompt(self, state: InvestigationState) -> str:
        """Test-only accessor — renders the prompt without invoking the
        LLM. Asserts the fast path does NOT short-circuit (pre-condition
        on the caller); the unit tests for the fast path read
        ``_is_no_document_case`` directly."""
        extraction = state.get("extraction_findings")
        if extraction is None:
            raise ValueError("_peek_rendered_prompt requires extraction_findings to be populated")
        return self._render_prompt(state, extraction, state["exception_type"])


def _fmt_optional(value: float | None) -> str:
    """Render a possibly-None numeric for prompt substitution.

    The validation prompts ask the LLM to reason over rates / magnitudes;
    an explicit ``"unknown"`` is more useful than a literal ``"None"``
    string when the upstream extraction couldn't surface the value.
    """
    if value is None:
        return "unknown"
    return f"{value}"


__all__ = [
    "AZURE_OPENAI_API_VERSION",
    "PROMPTS_DIR",
    "ValidationAgent",
]
