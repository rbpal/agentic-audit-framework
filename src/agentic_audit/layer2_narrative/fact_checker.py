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

from rapidfuzz import fuzz

from agentic_audit.models.evidence import ExtractedEvidence
from agentic_audit.models.narrative import FactCheckResult, NarrativeResponse

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

# rapidfuzz partial_ratio threshold for entity grounding. Empirically
# calibrated against the canonical punctuation-mismatch case
# ("ACME Inc" vs JSON-flattened "ACME, Inc." → score 87.5) and
# representative hallucinations ("ACME" vs "ACPI" → 67;
# "Globex Corp" vs unrelated blob → 29). 85 absorbs the former while
# rejecting the latter. Re-calibrate against task_07 sweep output if
# the false-positive / false-negative ratio drifts.
_FUZZ_THRESHOLD = 85


class FactChecker:
    """Deterministic post-generation verifier. Extracts numerics and
    entities from a narrative; checks each appears in the evidence
    JSON. Zero LLM calls.

    Stateless — instances are cheap; tests construct one per call.
    """

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
            if numeric not in evidence_blob:
                issues.append(f"numeric not in evidence: {numeric!r}")

        for entity in _ENTITY_RE.findall(narrative.narrative_text):
            if not self._entity_grounded(entity, evidence_blob):
                issues.append(f"entity not in evidence: {entity!r}")

        return FactCheckResult(passed=len(issues) == 0, issues=issues)

    @staticmethod
    def _entity_grounded(entity: str, evidence_blob: str) -> bool:
        """Exact substring check first (cheap), fuzzy match as fallback.

        ``partial_ratio`` slides the entity along the evidence blob and
        returns the best window's similarity score. We accept anything
        ≥ ``_FUZZ_THRESHOLD``.
        """
        if entity in evidence_blob:
            return True
        return fuzz.partial_ratio(entity, evidence_blob) >= _FUZZ_THRESHOLD

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
