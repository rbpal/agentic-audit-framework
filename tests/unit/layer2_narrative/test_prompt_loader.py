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
