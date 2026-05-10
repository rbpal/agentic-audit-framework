"""Tests for the LLM-as-judge prompt template (Step 6 task_02).

Mirrors ``test_prompt_loader.py``'s shape: load the template via the
existing ``load_prompt`` (version string ``"judge_v1.0"`` resolves to
``prompts/judge_v1_0.txt``), substitute fixture values, assert no
leftover placeholders + the three Decision Rules are preserved.
"""

from __future__ import annotations

import json
from string import Template

from agentic_audit.layer2_narrative.prompt_loader import PROMPTS_DIR, load_prompt

# ---------- load via existing loader ---------------------------------------


def test_judge_v1_0_loads_via_existing_load_prompt() -> None:
    """``load_prompt('judge_v1.0')`` resolves to ``judge_v1_0.txt`` —
    same dot-to-underscore filename rule as narrative prompts. No new
    loader function needed for Step 6."""
    text = load_prompt("judge_v1.0")
    assert text
    assert len(text) > 200  # not a stub


def test_judge_v1_0_filename_lives_under_prompts_dir() -> None:
    """Sanity: judge prompt sits next to the narrative prompt, not in
    a sibling folder."""
    assert (PROMPTS_DIR / "judge_v1_0.txt").is_file()


# ---------- template render ------------------------------------------------


def _sample_evidence_json() -> str:
    return json.dumps(
        {
            "engagement_id": "alpha-pension-fund-2025",
            "control_id": "DC-9",
            "quarter": "Q1",
            "attribute_id": "A",
            "attribute_check": {
                "status": "pass",
                "evidence_cell_refs": ["DC-9 Billing!r4c1"],
                "extracted_value": "AB — 2026-01-15",
            },
            "reviewer": {"initials": "CD", "role": "reviewer", "date": "2026-01-16"},
        }
    )


def test_judge_v1_0_template_renders_with_sample_inputs() -> None:
    """Headline acceptance test for task_02: load the shipped template,
    substitute every placeholder, get a complete prompt with no leftover
    ``${...}`` markers."""
    template = Template(load_prompt("judge_v1.0"))
    rendered = template.substitute(
        narrative_text="Preparer AB signed DC-9.A on 2026-01-15.",
        cited_fields='["DC-9 Billing!r4c1"]',
        evidence_json=_sample_evidence_json(),
        attribute_definition="DC-9.A — preparer signoff present",
        gold_expected_verdict="pass",
    )

    # Every placeholder consumed
    for placeholder in (
        "${narrative_text}",
        "${cited_fields}",
        "${evidence_json}",
        "${attribute_definition}",
        "${gold_expected_verdict}",
    ):
        assert placeholder not in rendered

    # Substituted values present
    assert "DC-9.A" in rendered
    assert "alpha-pension-fund-2025" in rendered
    assert "preparer signoff present" in rendered


def test_judge_v1_0_template_substitute_strict_mode_succeeds() -> None:
    """``Template.substitute`` (strict, not ``safe_substitute``) must
    accept exactly the five placeholders the prompt advertises — proves
    no stray ``${typo}`` got introduced."""
    template = Template(load_prompt("judge_v1.0"))
    template.substitute(
        narrative_text="x",
        cited_fields="[]",
        evidence_json="{}",
        attribute_definition="x",
        gold_expected_verdict="pass",
    )  # no KeyError == all placeholders accounted for


# ---------- Decision Rules preserved ---------------------------------------


def test_judge_v1_0_preserves_three_decision_rules() -> None:
    """The three rules from the design contract must literally appear
    in the prompt text. If a future edit drops one, the judge silently
    loses an invariant — catch the drift at test time, not at sweep
    time."""
    text = load_prompt("judge_v1.0")

    # Rule 1: pass/fail must cite specific evidence fields
    assert "cited_evidence_fields" in text
    assert "specific evidence" in text.lower()

    # Rule 2: default to "uncertain" when ambiguous
    assert '"uncertain"' in text
    assert "Do not guess" in text or "do not guess" in text

    # Rule 3: do NOT re-verify numeric grounding (FactChecker's job)
    assert "FactChecker" in text
    assert "numeric grounding" in text


def test_judge_v1_0_specifies_output_schema_fields() -> None:
    """The prompt must instruct the model to emit the four
    JudgeResponse fields. If the prompt drops one, pydantic will
    reject the response at parse time; catch the drift here."""
    text = load_prompt("judge_v1.0")
    for field in ("verdict", "confidence", "reasoning", "cited_evidence_fields"):
        assert field in text


def test_judge_v1_0_output_format_is_json_no_markdown() -> None:
    """Same ``response_format={'type': 'json_object'}`` posture as the
    narrative prompt — the LLM must NOT wrap output in markdown
    fences. Pin the prompt's instruction explicitly."""
    text = load_prompt("judge_v1.0")
    assert "Do not wrap the JSON in markdown fences" in text
