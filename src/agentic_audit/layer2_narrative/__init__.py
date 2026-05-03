"""Layer 2: Grounded narrative generation.

Reads ``ExtractedEvidence`` from ``audit_dev.silver.evidence`` (via
``SilverEvidenceReader``), invokes Azure OpenAI GPT-4o
(temperature=0, JSON mode) to produce a short grounded narrative for
each narratable attribute, validates citation via ``FactChecker``,
and writes results to ``audit_dev.gold.narratives``.

Layer 2 narrates 8 of the 10 (control, attribute) pairs per quarter.
The two it skips — DC-2.B (variance explanation) and DC-9.D
(rate-change amendment) — are also covered by Layer 3 (React loop),
which performs the qualitative judgment that complements Layer 1's
mechanical presence check. Layer 2 skips them to avoid producing two
narratives for the same attribute. Layer 2 and Layer 3 are parallel
silver consumers — neither hands off to the other; both read
``audit_dev.silver.evidence`` independently. See Decision 2 in
``privateDocs/step_05_layer2_narrative.md``.

Per Decision 6.2: prompt templates live as text files in ``prompts/``
versioned in git — no MLflow dependency. ``load_prompt(version)``
resolves a version string (e.g. ``"v1.0"``) to a filename.
"""

from agentic_audit.layer2_narrative.prompt_loader import (
    PROMPTS_DIR,
    load_prompt,
)

__all__ = [
    "PROMPTS_DIR",
    "load_prompt",
]
