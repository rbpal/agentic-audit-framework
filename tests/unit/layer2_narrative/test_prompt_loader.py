"""Tests for ``agentic_audit.layer2_narrative.prompt_loader`` plus the
shipped v1.0 prompt template renders correctly against a sample
``NarrativeRequest`` payload.

The render test is the headline acceptance check from
``step_05_layer2_narrative.md`` task_01: prove the template ships in a
state the generator (task_03) can drop straight into.
"""

from __future__ import annotations

import json
from string import Template

import pytest

from agentic_audit.layer2_narrative.prompt_loader import PROMPTS_DIR, load_prompt
from agentic_audit.models.narrative import NarrativeRequest

# ---------- load_prompt ----------------------------------------------------


def test_load_prompt_v1_0_returns_non_empty_text() -> None:
    text = load_prompt("v1.0")
    assert text
    assert len(text) > 100  # not a stub


def test_load_prompt_translates_dot_to_underscore() -> None:
    """'v1.0' resolves to 'v1_0.txt' on disk — same content, two paths."""
    via_loader = load_prompt("v1.0")
    via_disk = (PROMPTS_DIR / "v1_0.txt").read_text(encoding="utf-8")
    assert via_loader == via_disk


def test_load_prompt_rejects_empty_version() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        load_prompt("")


def test_load_prompt_raises_for_missing_version() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("v999.999")


def test_prompts_dir_is_under_layer2_package() -> None:
    """Sanity: PROMPTS_DIR resolves to the package's prompts/ folder, not
    a sibling. Relocating the package shouldn't break the loader silently.
    """
    assert PROMPTS_DIR.name == "prompts"
    assert PROMPTS_DIR.parent.name == "layer2_narrative"


# ---------- v1.0 template render ------------------------------------------


def _sample_evidence_json() -> str:
    return json.dumps(
        {
            "engagement_id": "alpha-pension-fund-2025",
            "control_id": "DC-9",
            "quarter": "Q1",
            "attribute_id": "A",
            "status": "pass",
            "evidence_cell_refs": ["DC-9 Billing!r4c1"],
            "extracted_value": "AB — 2026-01-15",
        }
    )


def test_v1_0_template_renders_with_sample_request() -> None:
    """The headline acceptance test for task_01: load the shipped
    template, substitute a real NarrativeRequest's fields, get a complete
    prompt with no leftover placeholders.
    """
    req = NarrativeRequest(
        control_id="DC-9",
        attribute_id="A",
        quarter="Q1",
        evidence_json=_sample_evidence_json(),
    )
    template = Template(load_prompt("v1.0"))
    rendered = template.substitute(
        control_id=req.control_id,
        attribute_id=req.attribute_id,
        quarter=req.quarter,
        evidence_json=req.evidence_json,
    )

    # All four placeholders consumed
    assert "${control_id}" not in rendered
    assert "${attribute_id}" not in rendered
    assert "${quarter}" not in rendered
    assert "${evidence_json}" not in rendered

    # Substituted values present
    assert "DC-9" in rendered
    assert "Q1" in rendered
    assert "alpha-pension-fund-2025" in rendered

    # Grounding-contract language preserved (sanity that we loaded the
    # right file, not an empty stub)
    assert "EVIDENCE JSON" in rendered
    assert "150 words" in rendered


def test_v1_0_template_uses_safe_substitute_compatible_placeholders() -> None:
    """Template.substitute (not safe_substitute) must succeed with all
    four placeholders — proves we haven't accidentally introduced a
    typo'd ${unknown_placeholder} that would silently slip through
    safe_substitute().
    """
    template = Template(load_prompt("v1.0"))
    template.substitute(
        control_id="DC-2",
        attribute_id="A",
        quarter="Q4",
        evidence_json="{}",
    )  # no KeyError == all placeholders accounted for


def test_v1_0_template_specifies_json_output_schema() -> None:
    """The prompt must instruct GPT-4o to emit narrative_text +
    cited_fields + word_count — these are the three NarrativeResponse
    fields. If the prompt drops one, the LLM may omit it, and pydantic
    will reject the response at parse time. Catch the prompt drift here,
    not at runtime in production.
    """
    text = load_prompt("v1.0")
    assert "narrative_text" in text
    assert "cited_fields" in text
    assert "word_count" in text


# ---------- v1.1 template (Step 5 follow-up #4) ----------------------------


def test_load_prompt_v1_1_returns_non_empty_text() -> None:
    """v1.1 ships as a sibling to v1.0 — both coexist for A/B comparison.
    The loader resolves 'v1.1' to 'v1_1.txt' on disk."""
    text = load_prompt("v1.1")
    assert text
    assert len(text) > 100  # not a stub


def test_v1_1_template_renders_with_sample_request() -> None:
    """v1.1 must accept the same 4 placeholders as v1.0 (same callers,
    same NarrativeRequest model). Anything else would force generator
    code changes per prompt version — exactly the coupling we avoid.
    """
    template = Template(load_prompt("v1.1"))
    template.substitute(
        control_id="DC-9",
        attribute_id="C",
        quarter="Q3",
        evidence_json="{}",
    )  # no KeyError == 4-placeholder contract preserved


def test_v1_1_template_specifies_same_json_output_schema_as_v1_0() -> None:
    """The output JSON schema (narrative_text, cited_fields, word_count)
    is the NarrativeResponse pydantic contract — must stay byte-identical
    across prompt versions or the parser breaks."""
    text = load_prompt("v1.1")
    assert "narrative_text" in text
    assert "cited_fields" in text
    assert "word_count" in text


def test_v1_1_template_requires_compact_attribute_identifier_usage() -> None:
    """Step 5 follow-up #4, fix #1: the v1.1 prompt instructs the model
    to use the compact dotted identifier ``${control_id}.${attribute_id}``
    (e.g., 'DC-9.E') and explicitly forbids the 'control ID and attribute
    X' phrasing that triggered the FactChecker tokenization false
    positive on DC-9 Q4 E in the v1.0 baseline.
    """
    text = load_prompt("v1.1")
    assert "ATTRIBUTE IDENTIFIER USAGE" in text
    # The compact form must be named explicitly.
    assert "${control_id}.${attribute_id}" in text
    # The forbidden phrasing must be called out (verbatim string match
    # so anyone diffing v1.0 vs v1.1 sees the constraint).
    assert "control ID and attribute" in text


def test_v1_1_template_qualifies_cross_file_dependent_attributes() -> None:
    """Step 5 follow-up #4, fix #2: the v1.1 prompt instructs the model
    to qualify status claims for cross-file-dependent attributes
    (DC-2.C and DC-9.C in the current corpus). Without this, the
    narrative inherits Layer-1's workpaper-internal status verbatim —
    which is what caused the DC-9 Q3 C semantic-only-fail (judge caught)
    AND the DC-2 Q3 C hidden FP (judge missed) in the v1.0 baseline.
    """
    text = load_prompt("v1.1")
    assert "CROSS-FILE-DEPENDENT ATTRIBUTES" in text
    # The two attributes the v1.0 baseline exposed as cross-file-dependent
    # must be named in the prompt — otherwise the LLM has no way to know
    # which attributes need qualification.
    assert "DC-2.C" in text
    assert "DC-9.C" in text
    # The qualification language itself must be present.
    assert "cross-file reconciliation" in text.lower()
