"""Layer 3 Narrative sub-agent (Step 7 task_06).

Reads ``ExtractionFindings`` + ``ValidationFindings`` + raw evidence
from the live ``InvestigationState`` and emits the final
``ExceptionNarrative`` (≤200 words, with ``recommendation ∈ {ACCEPT,
ESCALATE}``) that lands on ``gold.layer3_decisions``.

Composition over inheritance: this module does NOT subclass
``layer2_narrative.NarrativeGenerator``. Layer 2's class is wired to
the per-attribute ``AttributeNarrative`` shape, the v1.1 prompt
template under ``layer2_narrative/prompts/``, and a single
``ExtractedEvidence`` input. Layer 3's input is structurally richer
(extraction + validation findings + raw evidence) and its output is a
different model (``ExceptionNarrative`` carries ``recommendation``,
not ``cited_fields``). Subclassing would require overriding nearly
every method.

What IS reused:

- **``_build_azure_openai_client``** (Layer 2) — same managed-identity
  auth, same ``AzureOpenAI`` shape, same JSON-mode contract.
- **``FactChecker._numeric_grounded`` + ``._entity_grounded``** —
  the carefully-calibrated grounding helpers (rapidfuzz threshold 85,
  numeric-variant generation, entity stopword stripping). We compose
  them against a Layer-3-specific substrate that combines raw
  evidence + extraction findings + validation findings + the
  ``evidence_anchors`` cell-ref strings the Extraction agent emitted.
- **The word-limit retry pattern** — first call, if over the limit
  retry with a stricter prompt, on second overrun truncate. Same
  shape as ``NarrativeGenerator._call_with_word_limit_retry``, just
  parameterised at 200 words instead of 150.

Failure posture (mirrors ``ValidationAgent``):

- Empty content / malformed JSON / pydantic validation failure: one
  retry, then a deterministic ``ExceptionNarrative`` with
  ``recommendation="ESCALATE"`` and a diagnostic ``narrative_text``.
- Fact-check failure on first attempt: one retry with an explicit
  "ground every numeric and entity in the substrate" instruction.
  On second failure, fallback ESCALATE narrative.
- Word-limit overrun on both attempts: truncate. The narrative still
  ships — truncation is a known-acceptable degradation.

The supervisor's judge gate (Step 7 task_03 wiring) is the second
line of defence: even a successfully-fact-checked narrative gets
routed to escalate if the judge verdict isn't ``"pass"`` and the
confidence isn't above ``CONFIDENCE_THRESHOLD``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from agentic_audit.layer2_narrative.fact_checker import (
    _ENTITY_RE,
    _ENTITY_STOPWORDS,
    _NUMERIC_RE,
    FactChecker,
)
from agentic_audit.layer2_narrative.generator import (
    AZURE_OPENAI_API_VERSION,
    _build_azure_openai_client,
)
from agentic_audit.layer3_agents.state import (
    ExceptionNarrative,
    ExceptionType,
    ExtractionFindings,
    InvestigationState,
    ValidationFindings,
)
from agentic_audit.models.telemetry import CallUsage, UsageRecorder
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from openai import AzureOpenAI

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Word ceiling on the exception narrative. 200 vs Layer 2's 150 —
# exceptions need more room to discharge the recommendation: the
# narrative has to cite both the extraction findings (rate delta or
# variance magnitude) AND the validation reasoning (why the document
# does or doesn't authorise it) AND a one-line recommendation
# justification.
WORD_LIMIT: int = 200

# Token budget. Larger than Layer 2 (500) because ExceptionNarrative
# carries citations + recommendation + word_count alongside the
# narrative_text body, AND the body itself is up to 200 words. 800
# leaves comfortable headroom for the JSON envelope + a multi-citation
# array + a ~280-token narrative body without truncation.
_MAX_TOKENS: int = 800


# ── Layer-3 fact-check substrate ────────────────────────────────────


# Domain abbreviation glossary appended to the fact-check substrate.
# The LLM consistently expands "IMA" → "Investment Management Agreement"
# (same for IPS, AUM) when generating prose; the abbreviation itself is
# in the substrate but the expansion isn't, producing a fact-check
# false positive. Including both forms in the substrate lets the
# narrative use either without tripping grounding. Surfaced by the
# first task_06 live sweep against aoai-aaf-rbpal-dev (2026-05-13).
_ABBREVIATION_GLOSSARY: str = (
    "Domain abbreviation glossary (substrate-only, not a source of truth):\n"
    "  IMA = Investment Management Agreement\n"
    "  IPS = Investment Policy Statement\n"
    "  AUM = Assets Under Management\n"
    "  ToC = Test of Controls\n"
    "  bps = basis points\n"
)

# System threshold values appended to the substrate so a narrative that
# legitimately cites them ("validation confidence 0.86 exceeded the 0.7
# threshold") grounds. Not a hallucination — the value is what the
# supervisor's gate uses — but it isn't in any state field by default.
# Updated when CONFIDENCE_THRESHOLD changes in supervisor.py.
_SYSTEM_THRESHOLD_GLOSSARY: str = (
    "System thresholds (substrate-only):\n"
    "  CONFIDENCE_THRESHOLD = 0.7\n"
    "  WORD_LIMIT = 200\n"
    "  MAX_ITERATIONS = 3\n"
)


def _l3_fact_check_substrate(
    state: InvestigationState,
    extraction: ExtractionFindings,
    validation: ValidationFindings,
) -> str:
    """Build the search blob the Layer-3 fact-checker grounds against.

    Combines six sources so a narrative that legitimately cites any of
    them grounds:

    1. Raw ``ExtractedEvidence`` (preparer / reviewer / source path /
       per-attribute cell refs) — same payload Layer 2 grounds against.
    2. ``ExtractionFindings`` — rates, amendment text, variance
       magnitude / explanation. The narrative will quote these.
    3. ``ValidationFindings`` — the validation agent's reasoning
       carries paraphrases of the document language; the narrative
       may echo them.
    4. ``evidence_anchors`` — the cell-ref strings the Extraction
       agent emitted. Narratives cite these in ``citations``.
    5. **Domain abbreviation glossary** (``_ABBREVIATION_GLOSSARY``)
       — both the abbreviation and its expansion (e.g. ``"IMA"`` AND
       ``"Investment Management Agreement"``). Empirically the LLM
       expands abbreviations in narrative prose; including both
       forms unblocks grounding without dictating prompt phrasing.
    6. **System threshold glossary** (``_SYSTEM_THRESHOLD_GLOSSARY``)
       — the supervisor's ``CONFIDENCE_THRESHOLD`` etc. The narrative
       legitimately cites "exceeded the 0.7 threshold" but the value
       isn't in any state field by default.

    Layer 2's FactChecker substrate (single ``ExtractedEvidence``
    JSON dump) would reject any of #2–#6 as ungrounded. Building a
    custom blob is the right move; we keep the regex + threshold
    discipline that the FactChecker statics carry.
    """
    parts: list[str] = []
    parts.append(state["current_quarter_evidence"].model_dump_json())
    parts.append(state["prior_quarter_evidence"].model_dump_json())
    parts.append(extraction.model_dump_json())
    parts.append(validation.model_dump_json())
    # evidence_anchors are already in extraction.model_dump_json() but
    # also append explicitly in case the LLM cites them with slightly
    # different formatting (e.g. an extra space) — cheap belt-and-
    # braces, no double-grounding risk.
    parts.extend(extraction.evidence_anchors)
    parts.append(_ABBREVIATION_GLOSSARY)
    parts.append(_SYSTEM_THRESHOLD_GLOSSARY)
    return "\n".join(parts)


# Layer-3-specific stopword extension. Added on top of Layer 2's
# `_ENTITY_STOPWORDS` to absorb tokens that show up in the
# exception-narrative output but are NOT in the underlying substrate:
#
# - **Recommendation enum values** (``ACCEPT``, ``ESCALATE``) — the
#   prompt asks the LLM to set ``recommendation`` to one of these; the
#   narrative_text often re-states the choice ("Recommendation:
#   ESCALATE") and the entity regex catches the all-caps token.
# - **Common abbreviations** (``ID``, ``IMA``) — the prompt uses
#   "Investment Management Agreement" and "control_id" but the LLM
#   compresses to "IMA" and "control ID" in narrative prose, neither of
#   which fuzz-matches the substrate (rapidfuzz is case-sensitive and
#   2-char tokens don't fuzz reliably).
# - **Domain capitalised words from the prompt scaffold** (``Validation``,
#   ``Extraction``, ``Investigation``) — the prompt's section labels
#   ("Validation judgment:", "Extraction findings:") leak into the
#   narrative as proper-noun-shaped words.
# - **Sentence-initial transition adverbs missed by Layer 2's set**
#   (``Given``, ``Per``, ``Effective``, ``Per`` again, ``Since``,
#   ``Although``) — the same pattern Layer 2 fixed for "However" /
#   "Therefore" / etc., extended for the words that surface in
#   exception-narrative phrasing.
#
# Re-calibrate against future live runs if new false-positive entities
# emerge — the stopword set is empirical, not theoretical.
_L3_EXTRA_ENTITY_STOPWORDS: frozenset[str] = frozenset(
    {
        # Recommendation enum names
        "ACCEPT",
        "ESCALATE",
        # Abbreviations
        "ID",
        "IMA",
        "IPS",
        "AUM",
        "Q1",
        "Q2",
        "Q3",
        "Q4",
        # Layer-3 prompt-scaffold capitalised words
        "Validation",
        "Extraction",
        "Narrative",
        "Investigation",
        "Recommendation",
        "Findings",
        "Judgment",
        # Sentence-initial transition adverbs (Layer 2's set extended)
        "Given",
        "Per",
        "Since",
        "Although",
        "Because",
        "Despite",
        "Based",
    }
)

_L3_ENTITY_STOPWORDS: frozenset[str] = _ENTITY_STOPWORDS | _L3_EXTRA_ENTITY_STOPWORDS


def _l3_fact_check(narrative_text: str, substrate: str) -> tuple[bool, list[str]]:
    """Run the FactChecker grounding helpers against a custom Layer-3
    substrate. Returns ``(passed, issues)`` — same shape as
    ``FactCheckResult`` but without the model wrapper since callers
    only need the boolean + diagnostic list.

    Reuses ``FactChecker._numeric_grounded`` + ``._entity_grounded``
    + the regex constants verbatim — the 0–1 → percent-equivalent
    translation, fuzzy entity match at threshold 85, the negative-
    lookaround anchor pattern all carry over. The stopword set is
    extended (``_L3_ENTITY_STOPWORDS``) to absorb the additional
    Layer-3-specific tokens that show up in exception narratives but
    not in the substrate.
    """
    issues: list[str] = []

    for numeric in _NUMERIC_RE.findall(narrative_text):
        if not FactChecker._numeric_grounded(numeric, substrate):
            issues.append(f"numeric not in evidence: {numeric!r}")

    for entity in _ENTITY_RE.findall(narrative_text):
        cleaned = _strip_leading_stopwords(entity)
        if not cleaned:
            continue
        if not FactChecker._entity_grounded(cleaned, substrate):
            issues.append(f"entity not in evidence: {cleaned!r}")

    return len(issues) == 0, issues


def _strip_leading_stopwords(entity: str) -> str:
    """Layer-3 variant of ``FactChecker._strip_leading_stopwords``.

    Peels sentence-initial stopwords from a captured entity using the
    extended Layer-3 set (``_L3_ENTITY_STOPWORDS``). Inlined rather
    than touching Layer 2's static so Layer 2 can refactor freely
    without breaking us, and so the Layer-3-specific stopwords
    (``ESCALATE``, ``IMA``, etc.) don't leak into Layer 2's narrative
    grounding."""
    tokens = entity.split()
    while tokens and tokens[0] in _L3_ENTITY_STOPWORDS:
        tokens.pop(0)
    return " ".join(tokens)


# ── NarrativeAgent ───────────────────────────────────────────────────


class NarrativeAgent:
    """Single-call (with retries) LLM narrative generator for Layer 3
    exception verdicts.

    Pinned to a deployment + prompt version at construction. Renders
    the per-``exception_type`` exception-narrative prompt with the
    extracted facts + validation reasoning, calls Azure OpenAI in
    JSON mode, parses into ``ExceptionNarrative``, retries on
    over-the-word-limit, fact-checks against the Layer-3 substrate,
    and returns. On any unrecoverable failure returns a deterministic
    fallback ESCALATE narrative — never raises into the supervisor.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str = "gpt-4o",
        prompt_version: str = "v1.1",
        client: AzureOpenAI | None = None,
        api_version: str = AZURE_OPENAI_API_VERSION,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        """Build a Narrative sub-agent.

        ``prompt_version`` defaults to ``"v1.1"`` to match Step 5
        follow-up #4's bump — the Layer-2 generator landed on v1.1 as
        its default; Layer 3 starts there too. Each ``NarrativeAgent``
        holds one version and resolves the per-exception-type variant
        by appending the type-specific suffix to the filename
        (``narrative_v1_1_exception_dc9d.txt``,
        ``narrative_v1_1_exception_dc2b.txt``).

        ``usage_recorder`` is optional; when supplied, every billed
        ``chat.completions.create`` call (1–4 per invocation depending
        on word-limit + fact-check retries) records its token usage on
        the recorder. Mirrors ``ValidationAgent`` + Layer-2 ``Judge``.
        Fallback ESCALATE narratives that fire without an LLM call
        leave the recorder untouched.
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
        prompt_version: str = "v1.1",
        endpoint_env_var: str = "AZURE_OPENAI_ENDPOINT",
        usage_recorder: UsageRecorder | None = None,
    ) -> NarrativeAgent:
        """Build from ``AZURE_OPENAI_ENDPOINT``. Same env contract as
        ``Judge.from_env`` and ``ExtractionAgent.from_env``."""
        endpoint = os.environ.get(endpoint_env_var)
        if not endpoint:
            raise RuntimeError(
                f"environment variable {endpoint_env_var!r} is not set; "
                "either export it (e.g. "
                "https://aoai-aaf-rbpal-dev.openai.azure.com/) or pass "
                "endpoint explicitly to NarrativeAgent(...)."
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

    @traced_function("layer3.narrative_agent.invoke")
    def invoke(self, state: InvestigationState) -> ExceptionNarrative:
        """Produce the final ``ExceptionNarrative`` for this
        investigation.

        Pre-condition: both ``extraction_findings`` and
        ``validation_findings`` are populated. The supervisor only
        routes to narrative after extraction + validation have run;
        a missing field surfaces as ``ValueError`` (routing bug).
        """
        narrative, _raw = self._invoke_with_raw_response(state)
        return narrative

    def _invoke_with_raw_response(
        self, state: InvestigationState
    ) -> tuple[ExceptionNarrative, str | None]:
        """Diagnostic variant of ``invoke`` — returns narrative PLUS
        the raw LLM JSON content (or ``None`` if the fallback fired
        without an LLM call).

        The slow integration test prints the raw content for operator
        review (mirrors Validation + Extraction agents' diagnostic
        helpers).
        """
        extraction = state.get("extraction_findings")
        validation = state.get("validation_findings")
        if extraction is None or validation is None:
            raise ValueError(
                "NarrativeAgent.invoke: state is missing extraction_findings "
                "or validation_findings. Supervisor must route through "
                "extraction + validation first; calling narrative on an "
                "incomplete state is a routing bug."
            )

        prompt = self._render_prompt(state, extraction, validation, state["exception_type"])
        substrate = _l3_fact_check_substrate(state, extraction, validation)

        try:
            narrative, raw = self._invoke_with_word_limit_retry(prompt)
        except (ValueError, ValidationError) as exc:
            logger.warning("NarrativeAgent LLM/parse failure; falling back to ESCALATE: %s", exc)
            return self._fallback_escalate(f"LLM/parse failure: {exc}"), None

        passed, issues = _l3_fact_check(narrative.narrative_text, substrate)
        if passed:
            return narrative, raw

        # One fact-check retry with explicit grounding instruction.
        logger.info(
            "NarrativeAgent fact-check failed on attempt 1 (%d issues); retrying with "
            "explicit grounding instruction",
            len(issues),
        )
        stricter = _build_grounding_retry_prompt(prompt, issues)
        try:
            narrative_2, raw_2 = self._invoke_with_word_limit_retry(stricter)
        except (ValueError, ValidationError) as exc:
            logger.warning(
                "NarrativeAgent fact-check retry parse failure; falling back to ESCALATE: %s",
                exc,
            )
            return self._fallback_escalate(f"fact-check retry parse failure: {exc}"), raw

        passed_2, issues_2 = _l3_fact_check(narrative_2.narrative_text, substrate)
        if passed_2:
            return narrative_2, raw_2

        logger.warning(
            "NarrativeAgent fact-check failed on both attempts (%d, %d issues); falling "
            "back to ESCALATE",
            len(issues),
            len(issues_2),
        )
        return (
            self._fallback_escalate(
                f"fact-check failure x2: attempt1={issues[:3]}; attempt2={issues_2[:3]}"
            ),
            raw_2,
        )

    # ── Prompt rendering ────────────────────────────────────────────

    def _render_prompt(
        self,
        state: InvestigationState,
        extraction: ExtractionFindings,
        validation: ValidationFindings,
        exception_type: ExceptionType,
    ) -> str:
        """Render the per-exception-type narrative prompt template.

        Filename convention: ``narrative_<version>_exception_<suffix>.txt``
        where ``suffix`` is the lowercase scope tag — ``dc9d`` for
        billing_rate_change, ``dc2b`` for variance_plausibility. Tags
        chosen for symmetry with the corpus naming + privateDocs §
        task_06 spec.
        """
        suffix = _PROMPT_SUFFIX_BY_EXCEPTION_TYPE[exception_type]
        filename = f"narrative_{self._prompt_version.replace('.', '_')}_exception_{suffix}.txt"
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
                evidence_anchors=", ".join(extraction.evidence_anchors) or "(none)",
                validation_is_authorized=str(validation.is_authorized),
                validation_confidence=f"{validation.confidence}",
                validation_reasoning=validation.reasoning,
                word_limit=str(WORD_LIMIT),
            )
        # variance_plausibility
        return Template(template_text).substitute(
            engagement_id=state["engagement_id"],
            control_id=state["control_id"],
            attribute_id=state["attribute_id"],
            quarter=state["quarter"],
            variance_magnitude=_fmt_optional(extraction.variance_magnitude),
            variance_explanation_text=extraction.variance_explanation_text or "",
            evidence_anchors=", ".join(extraction.evidence_anchors) or "(none)",
            validation_is_authorized=str(validation.is_authorized),
            validation_confidence=f"{validation.confidence}",
            validation_reasoning=validation.reasoning,
            word_limit=str(WORD_LIMIT),
        )

    # ── LLM call + word-limit retry ─────────────────────────────────

    def _invoke_with_word_limit_retry(self, prompt: str) -> tuple[ExceptionNarrative, str]:
        """Mirror of ``NarrativeGenerator._call_with_word_limit_retry``.

        First attempt: parse the LLM response into a raw payload dict
        (NOT yet into ``ExceptionNarrative`` — its ``word_count`` field
        has a ``le=200`` validator that would reject any over-limit
        response, short-circuiting the retry/truncation logic). The
        actual word count is derived from ``narrative_text.split()``
        rather than trusted from the payload — the LLM occasionally
        misreports its own count.

        If actual count exceeds ``WORD_LIMIT``, retry with a stricter
        prompt; on second overrun, truncate to ``WORD_LIMIT`` words.
        Then validate-and-construct ``ExceptionNarrative`` once at the
        end with the post-truncation count, guaranteeing the contract
        the gold table sees holds.
        """
        payload, raw = self._invoke_llm_json(prompt)
        actual_count = len(payload.get("narrative_text", "").split())
        if actual_count <= WORD_LIMIT:
            return self._build_narrative_from_payload(payload, actual_count), raw

        logger.info(
            "Layer-3 narrative exceeded %d words on attempt 1 (%d words); retrying",
            WORD_LIMIT,
            actual_count,
        )
        stricter = (
            f"{prompt}\n\n"
            f"PREVIOUS RESPONSE WAS {actual_count} WORDS, OVER THE "
            f"{WORD_LIMIT}-WORD LIMIT. PRODUCE A SHORTER NARRATIVE STRICTLY "
            f"UNDER {WORD_LIMIT} WORDS. SAME JSON SCHEMA."
        )
        payload_2, raw_2 = self._invoke_llm_json(stricter)
        actual_count_2 = len(payload_2.get("narrative_text", "").split())
        if actual_count_2 <= WORD_LIMIT:
            return self._build_narrative_from_payload(payload_2, actual_count_2), raw_2

        logger.warning(
            "Layer-3 narrative exceeded %d words on both attempts (%d, %d); truncating",
            WORD_LIMIT,
            actual_count,
            actual_count_2,
        )
        truncated_words = payload_2.get("narrative_text", "").split()[:WORD_LIMIT]
        truncated_payload = dict(payload_2)
        truncated_payload["narrative_text"] = " ".join(truncated_words)
        truncated_payload["word_count"] = len(truncated_words)
        return self._build_narrative_from_payload(truncated_payload, len(truncated_words)), raw_2

    @staticmethod
    def _build_narrative_from_payload(
        payload: dict[str, Any], actual_word_count: int
    ) -> ExceptionNarrative:
        """Construct ``ExceptionNarrative`` from a verified payload.

        Overwrites ``word_count`` with the count derived from the
        actual ``narrative_text`` rather than trusting the LLM's
        self-report — the schema's ``le=200`` validator will reject if
        the actual count exceeds the limit (which the caller has
        ensured by the time we get here, either by no overrun or by
        truncation)."""
        normalised = dict(payload)
        normalised["word_count"] = actual_word_count
        return ExceptionNarrative(**normalised)

    def _invoke_llm_json(self, prompt: str) -> tuple[dict[str, Any], str]:
        """Single chat completion in JSON mode → raw parsed dict.

        Returns ``(payload_dict, raw_content_str)``. Single retry on
        empty content or ``json.JSONDecodeError``. Raises ``ValueError``
        on the second failure — the outer caller wraps this in the
        fallback ESCALATE branch.

        Pydantic validation is **not** performed here; the caller
        builds the ``ExceptionNarrative`` after word-limit
        normalisation so an over-limit response can be retried /
        truncated rather than rejected outright (the schema enforces
        ``word_count <= 200`` which would otherwise short-circuit).
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
                if attempt == 1:
                    logger.warning(
                        "Layer-3 narrative empty content on attempt 1 (finish_reason=%r); retrying",
                        response.choices[0].finish_reason,
                    )
                    continue
                raise ValueError(
                    f"empty narrative response from {self._deployment!r} on both "
                    f"attempts (finish_reason="
                    f"{response.choices[0].finish_reason!r})"
                )
            try:
                return json.loads(content), content
            except json.JSONDecodeError as exc:
                if attempt == 1:
                    logger.warning(
                        "Layer-3 narrative JSON parse failed on attempt 1 (%s); retrying",
                        exc.msg,
                    )
                    continue
                raise ValueError(
                    f"malformed narrative JSON from {self._deployment!r} on both "
                    f"attempts: {exc.msg}; raw content[:500]={content[:500]!r}"
                ) from exc
        raise RuntimeError("unreachable: _invoke_llm_json exhausted both attempts")

    def _record_usage_if_present(self, response: Any) -> None:
        """Record per-call token usage on the recorder if one is wired.

        Mirrors ``ValidationAgent._record_usage_if_present`` and the
        Layer-2 Judge — recording fires after every billed API call,
        regardless of parse outcome (token usage is what the cloud
        meters, and a malformed-JSON response was still a billed call).
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

    # ── Fallback ────────────────────────────────────────────────────

    @staticmethod
    def _fallback_escalate(reason: str) -> ExceptionNarrative:
        """Build the deterministic ESCALATE fallback narrative.

        Mirrors the degraded-escalation shape Step 7 task_07 will
        formalise — same canned ``narrative_text``, same
        ``recommendation="ESCALATE"``, same empty citations. Reason
        text appended for operator triage; downstream consumers can
        grep ``gold.layer3_decisions`` for the failure mode.
        """
        return ExceptionNarrative(
            narrative_text=(
                f"Automated investigation could not produce a grounded narrative; "
                f"human review required ({reason})."
            ),
            citations=[],
            recommendation="ESCALATE",
            word_count=0,
        )

    # ── Test utility ────────────────────────────────────────────────

    def _peek_rendered_prompt(self, state: InvestigationState) -> str:
        """Test-only accessor — renders the prompt without invoking the
        LLM. Asserts both extraction + validation findings are populated;
        unit tests that exercise the no-findings ValueError go through
        ``invoke`` directly."""
        extraction = state.get("extraction_findings")
        validation = state.get("validation_findings")
        if extraction is None or validation is None:
            raise ValueError("_peek_rendered_prompt requires extraction + validation findings")
        return self._render_prompt(state, extraction, validation, state["exception_type"])


# ── Helpers ──────────────────────────────────────────────────────────


_PROMPT_SUFFIX_BY_EXCEPTION_TYPE: dict[ExceptionType, str] = {
    "billing_rate_change": "dc9d",
    "variance_plausibility": "dc2b",
}


def _fmt_optional(value: float | None) -> str:
    """Render a possibly-None numeric for prompt substitution. Same
    helper as ValidationAgent's — kept inlined rather than imported
    to avoid a horizontal Layer 3 → Layer 3 module dependency for one
    formatting call."""
    if value is None:
        return "unknown"
    return f"{value}"


def _build_grounding_retry_prompt(original_prompt: str, issues: list[str]) -> str:
    """Append a grounding instruction listing the un-grounded tokens.

    Surfaces the FactChecker's diagnostic so the LLM can target its
    rewrite — vague "ground every claim" instructions produce
    cosmetic edits; naming the exact ungrounded tokens in the
    follow-up has empirically driven the model to either drop them
    or restate them with a verbatim citation.
    """
    issue_list = "\n".join(f"  - {issue}" for issue in issues[:8])
    return (
        f"{original_prompt}\n\n"
        f"PREVIOUS RESPONSE FAILED FACT-CHECK. The following claims could not "
        f"be verified against the supplied evidence:\n{issue_list}\n\n"
        f"Rewrite the narrative so EVERY numeric value and EVERY proper noun "
        f"appears verbatim in the evidence/extraction/validation payload above. "
        f"Drop unverifiable claims rather than paraphrasing them. SAME JSON "
        f"SCHEMA, still under {WORD_LIMIT} words."
    )


__all__ = [
    "AZURE_OPENAI_API_VERSION",
    "PROMPTS_DIR",
    "WORD_LIMIT",
    "NarrativeAgent",
]
