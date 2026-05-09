"""Deterministic post-generation verifier for Layer 2 narratives.

`NarrativeGenerator.generate()` produces a `NarrativeResponse` that is
JSON-valid, schema-valid, under 150 words, and grounded by prompt
instruction. None of those properties prove the narrative is
**factually grounded in the evidence**. JSON mode only proves shape;
pydantic only proves types; the word-limit retry only proves length.

`FactChecker` is the deterministic check that closes the loop: every
numeric and every entity in the narrative must appear in the evidence
JSON. Zero LLM calls — regex extraction + rapidfuzz lexical matching.

Asymmetric matching by token type:

- **Numerics** get exact-substring match. A hallucinated `$2.5M` is
  wrong even if the evidence says `$2.4M`; fuzzy matching here would
  mask real errors.
- **Entities** get rapidfuzz `partial_ratio` at threshold 85
  (empirically calibrated — see ``_FUZZ_THRESHOLD`` below). The LLM
  may emit `ACME Inc` when evidence says `ACME, Inc.` — that is
  stylistic, not hallucinatory. Exact match would flood `issues` with
  whitespace / punctuation noise.

See ``privateDocs/step_05_layer2_narrative.md`` (Step 5 task_05
pre-execution notes) for the design rationale and rejected
alternatives (LLM-as-fact-checker, embedding similarity, stdlib
`difflib`).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from rapidfuzz import fuzz

from agentic_audit.models.evidence import ExtractedEvidence
from agentic_audit.models.narrative import FactCheckResult, NarrativeResponse
from agentic_audit.observability import traced_function

# Numerics: dollar amounts, percentages, and plain integers/decimals.
# Three alternatives in priority order so "$1,250" matches as a
# single dollar-amount token (not "1,250") and "75%" matches with the
# percent sign attached (not "75"). Examples:
#   "$1,000.00" → matches via alt 1
#   "75%"       → matches via alt 2
#   "150"       → matches via alt 3
_NUMERIC_RE = re.compile(r"\$\d+(?:[.,]\d+)*|\d+(?:[.,]\d+)*%|\b\d+(?:[.,]\d+)*\b")

# Entities: capitalised tokens / acronyms / hyphenated identifiers.
# Examples: "Q1", "DC-9", "ACME", "ACME Inc", "JPMorgan Chase".
# Multi-word entities preserved by the trailing optional group; the
# regex engine is greedy so "ACME Inc" matches as one entity not two.
_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:[-\s][A-Z][A-Za-z0-9]*)*\b")

# Sentence-initial common words that the entity regex inevitably
# matches but are NOT real entities (just standard English
# capitalised at start of a sentence). The step_05_task_07 sweep
# revealed these as the dominant false-positive source — every
# narrative starting with "The reconciliation passed..." or "Notes
# confirm..." was failing fact-check on tokens like "The", "Notes",
# "No", "Rows" instead of any real grounding issue. Filtering them
# pre-check eliminates the false-positive flood without weakening
# detection of real hallucinations.
#
# Keep this list conservative — only common English function words,
# determiners, and generic nouns observed in actual gpt-4o output.
# Domain-specific terms (control names, product names, firm names)
# stay through the filter and ARE checked against evidence.
_ENTITY_STOPWORDS: frozenset[str] = frozenset(
    {
        # Determiners / articles
        "The",
        "A",
        "An",
        "This",
        "That",
        "These",
        "Those",
        # Quantifiers / negations
        "No",
        "Yes",
        "All",
        "None",
        "Some",
        "Any",
        "Each",
        "Every",
        # Conjunctions / prepositions (sentence-initial)
        "And",
        "Or",
        "But",
        "For",
        "Of",
        "In",
        "On",
        "At",
        "By",
        "With",
        "Without",
        "From",
        "To",
        # Common audit-prose nouns observed in gpt-4o narratives
        "Notes",
        "Rows",
        "Status",
        "Reconciliation",
        "Reviewer",
        "Preparer",
        "Evidence",
        "Variance",
        "Attribute",
        "Quarter",
        "Control",
        "Period",
        "Sign",
        "Sign-Off",
        "Auditor",
        # Time-of-action words
        "During",
        "Before",
        "After",
        "When",
        "While",
        # Verb-as-sentence-start (rare but observed)
        "Confirms",
        "Identifies",
        "Indicates",
        "Reflects",
        "Shows",
        "Verifies",
        "Demonstrates",
        # Generic timezone / unit codes — the LLM sometimes appends
        # "UTC" to a date for clarity (e.g. "as of 2025-05-27 UTC").
        # The token is capitalised so it survives the entity regex,
        # but it's not a domain entity and won't appear in the JSON
        # evidence blob. Surfaced by the first task_07 calibration
        # sweep (DC-2 Q2 D and DC-2 Q4 D both flagged 'UTC' alone).
        "UTC",
        "GMT",
        "EST",
        "PST",
    }
)

# rapidfuzz partial_ratio threshold for entity grounding. Empirically
# calibrated against the canonical punctuation-mismatch case
# ("ACME Inc" vs JSON-flattened "ACME, Inc." → score 87.5) and
# representative hallucinations ("ACME" vs "ACPI" → 67;
# "Globex Corp" vs unrelated blob → 29). 85 absorbs the former while
# rejecting the latter. Re-calibrate against task_07 sweep output if
# the false-positive / false-negative ratio drifts.
_FUZZ_THRESHOLD = 85


def _numeric_variants(numeric: str) -> list[str]:
    """Generate equivalent string representations of a numeric token.

    Surfaced by the first task_07 sweep: DC-9 attribute F narratives
    consistently flagged ``40.0%``, ``30.0%``, ``100.0%`` as ungrounded
    even though the silver evidence contained the equivalent decimals
    ``0.4``, ``0.3``, ``1.0``. The fact-checker's literal-string match
    couldn't recognise the equivalence; this helper expands a single
    numeric into the alternative forms a JSON-serialised evidence blob
    might hold.

    Asymmetric translation:

    - Percent → decimal: ``40.0%`` → ``["0.4", "0"]`` (when whole) or
      ``75%`` → ``["0.75"]``. The form ``str(float(...))`` matches
      Python ``json.dumps`` output exactly.
    - Decimal → percent: ``0.4`` → ``["40%", "40.0%"]``. Only applied
      to decimals in ``(0, 1]`` — outside that range the
      decimal-as-percentage equivalence is unsafe.

    Returns an empty list for tokens that don't fit either pattern
    (dollar amounts, plain integers, decimals > 1, garbage) — those
    get string-match-only behaviour.

    Uses ``Decimal`` for arithmetic to avoid float precision artefacts
    (``0.3 * 100`` returns ``30.000000000000004`` in float; via
    Decimal it returns exactly ``Decimal('30.00')``).
    """
    variants: list[str] = []

    if numeric.endswith("%"):
        bare = numeric[:-1].strip().replace(",", "")
        try:
            value = Decimal(bare)
        except (InvalidOperation, ValueError):
            return variants
        decimal_val = value / Decimal("100")
        # Bare-number form: ``40.0%`` → ``"40.0"`` (covers evidence
        # that stores percent VALUES as numbers, with the unit
        # semantics implied by the field name — e.g. silver JSON
        # ``"effective_pcts":[40.0,30.0,30.0]``). This was the dominant
        # DC-9 F failure pattern after the percent↔decimal fix landed:
        # the LLM faithfully renders ``effective_pcts`` as ``40.0%``,
        # but the evidence has ``40.0`` not ``0.4``.
        try:
            bare_form = str(float(value))
        except (OverflowError, ValueError):
            bare_form = ""
        if bare_form:
            variants.append(bare_form)
        # Whole-number bare form: ``40%`` → ``"40"`` (integer),
        # complementing ``"40.0"``.
        if value == value.to_integral_value():
            variants.append(str(int(value)))
        # Decimal-equivalent form: ``40.0%`` → ``"0.4"``. ``str(float(...))``
        # produces Python's shortest round-trip representation —
        # exactly what ``json.dumps`` emits for equivalent floats.
        try:
            float_form = str(float(decimal_val))
        except (OverflowError, ValueError):
            return variants
        variants.append(float_form)
        # Whole-number form: ``100%`` → ``1`` (in addition to ``1.0``)
        # so evidence storing whole percents as bare integers matches.
        if decimal_val == decimal_val.to_integral_value():
            variants.append(str(int(decimal_val)))
    elif "$" not in numeric and "%" not in numeric:
        bare = numeric.replace(",", "")
        try:
            value = Decimal(bare)
        except (InvalidOperation, ValueError):
            return variants
        # Only translate small decimals — a bare ``5`` is not
        # ambiguously ``5%`` (it's just five). The (0, 1] range is the
        # safe zone where decimal-as-percentage is the natural
        # auditor-prose expectation.
        if Decimal("0") < value <= Decimal("1"):
            percent_decimal = value * Decimal("100")
            try:
                percent_float = float(percent_decimal)
            except (OverflowError, ValueError):
                return variants
            if percent_float == int(percent_float):
                # Whole-percent input: write both ``40%`` and ``40.0%``
                # forms so either narrative shape matches evidence.
                variants.append(f"{int(percent_float)}%")
                variants.append(f"{percent_float:.1f}%")
            else:
                # Fractional percent — canonical form only
                variants.append(f"{percent_float}%")

    return variants


class FactChecker:
    """Deterministic post-generation verifier. Extracts numerics and
    entities from a narrative; checks each appears in the evidence
    JSON. Zero LLM calls.

    Stateless — instances are cheap; tests construct one per call.
    """

    @traced_function("layer2.fact_checker.check")
    def check(
        self,
        narrative: NarrativeResponse,
        evidence: ExtractedEvidence,
    ) -> FactCheckResult:
        """Verify every numeric and entity in ``narrative.narrative_text``
        appears in the JSON-flattened ``evidence``. Returns a
        ``FactCheckResult`` whose ``issues`` list names every ungrounded
        token (empty list if all grounded → ``passed=True``).
        """
        evidence_blob = self._evidence_as_string(evidence)
        issues: list[str] = []

        for numeric in _NUMERIC_RE.findall(narrative.narrative_text):
            if not self._numeric_grounded(numeric, evidence_blob):
                issues.append(f"numeric not in evidence: {numeric!r}")

        for entity in _ENTITY_RE.findall(narrative.narrative_text):
            cleaned = self._strip_leading_stopwords(entity)
            if not cleaned:
                # Entity was only stopwords (e.g. "The", "No"). Skip.
                continue
            if not self._entity_grounded(cleaned, evidence_blob):
                issues.append(f"entity not in evidence: {cleaned!r}")

        return FactCheckResult(passed=len(issues) == 0, issues=issues)

    @staticmethod
    def _numeric_grounded(numeric: str, evidence_blob: str) -> bool:
        """Verify a numeric is in evidence, allowing for equivalent
        representations.

        The first task_07 sweep revealed that LLM narratives express
        decimals as percentages for human readability (e.g. silver
        stores ``effective_rate = 0.40``, narrative writes ``40.0%``)
        and our literal-string matcher couldn't equate the two. Direct
        string-match is tried first (cheap); equivalent forms are
        generated only when that misses.

        Asymmetric: numerics with a ``%`` suffix get decimal
        equivalents tried; small decimals (0 < x ≤ 1) get percent
        equivalents tried. Dollar amounts and integer counts are NOT
        translated — there's no canonical equivalence to apply, and
        false-equating $-amounts to bare numerics would mask real
        hallucinations.

        Variants use a regex with negative-lookaround anchors
        (``(?<!\\d)variant(?!\\d)``) so that, e.g., narrative ``40%``
        → variant ``0.4`` does NOT falsely match evidence ``0.45``,
        but DOES match evidence ending with a sentence period like
        ``"rate 0.4."``. We block adjacent digits only — adjacent
        dots (sentence punctuation, JSON delimiters) are fine.
        """
        if numeric in evidence_blob:
            return True
        for variant in _numeric_variants(numeric):
            pattern = r"(?<!\d)" + re.escape(variant) + r"(?!\d)"
            if re.search(pattern, evidence_blob):
                return True
        return False

    @staticmethod
    def _strip_leading_stopwords(entity: str) -> str:
        """Peel sentence-initial stopwords from a captured entity.

        The entity regex greedily matches multi-word capitalised
        sequences, so "The ACME Inc" comes through as a single token.
        Naive ``entity in _ENTITY_STOPWORDS`` misses it. This helper
        splits on whitespace, drops leading tokens that match the
        stopword list, and returns the remainder joined back. Returns
        empty string if the entity was *only* stopwords (e.g. "The"
        alone), which the caller treats as "skip".
        """
        tokens = entity.split()
        while tokens and tokens[0] in _ENTITY_STOPWORDS:
            tokens.pop(0)
        return " ".join(tokens)

    @staticmethod
    def _entity_grounded(entity: str, evidence_blob: str) -> bool:
        """Exact substring check first (cheap), fuzzy match as fallback.

        ``partial_ratio`` slides the entity along the evidence blob and
        returns the best window's similarity score. We accept anything
        ≥ ``_FUZZ_THRESHOLD``.
        """
        if entity in evidence_blob:
            return True
        score: float = fuzz.partial_ratio(entity, evidence_blob)
        return score >= _FUZZ_THRESHOLD

    @staticmethod
    def _evidence_as_string(evidence: ExtractedEvidence) -> str:
        """Flatten the evidence into a single search blob.

        ``model_dump_json`` is the simplest deterministic flattener —
        every field, every nested attribute, no mutable iteration order
        risk. The JSON delimiters do mean entity matches sometimes
        straddle a quote/colon (e.g. ``"name":"ACME"``); the fuzzy
        threshold is set to absorb that without manual stripping.
        """
        return evidence.model_dump_json()


__all__ = [
    "FactChecker",
]
