"""Tests for the v2 engagement gold-answer schema (Step 2)."""

from __future__ import annotations

from pathlib import Path

from agentic_audit.models.engagement import (
    EngagementSpec,
    QuarterControlSpec,
)
from agentic_audit.models.engagement_gold_answer import (
    EngagementGoldAnswer,
    build_all_gold_answers,
    build_quarter_gold_answer,
    engagement_gold_answer_to_json,
    load_engagement_gold_answer,
)


def _plan_engagement() -> EngagementSpec:
    quarters = (
        QuarterControlSpec(control_id="DC-2", quarter="Q1", defect="none"),
        QuarterControlSpec(control_id="DC-2", quarter="Q2", defect="none"),
        QuarterControlSpec(
            control_id="DC-2", quarter="Q3", defect="dc2_variance_explanation_inadequate"
        ),
        QuarterControlSpec(control_id="DC-2", quarter="Q4", defect="dc2_variance_no_explanation"),
        QuarterControlSpec(control_id="DC-9", quarter="Q1", defect="none"),
        QuarterControlSpec(
            control_id="DC-9", quarter="Q2", defect="dc9_rate_change_with_amendment"
        ),
        QuarterControlSpec(control_id="DC-9", quarter="Q3", defect="dc9_figure_mismatch"),
        QuarterControlSpec(
            control_id="DC-9", quarter="Q4", defect="dc9_rate_change_without_amendment"
        ),
    )
    return EngagementSpec(
        entity_name="Alpha Pension Fund",
        year=2025,
        seed=2025,
        quarters=quarters,
    )


# ── Clean quarter → every attribute passes ───────────────────────────


def test_clean_dc9_quarter_all_attributes_pass() -> None:
    ans = build_quarter_gold_answer(_plan_engagement(), "DC-9", "Q1")
    assert ans.defect == "none"
    assert ans.expected_attribute_results == {
        "DC-9.A": "pass",
        "DC-9.B": "pass",
        "DC-9.C": "pass",
        "DC-9.D": "pass",
        "DC-9.E": "pass",
        "DC-9.F": "pass",
    }
    assert ans.expected_quarter_verdict == "Effective"
    assert ans.expected_cross_file_contradiction is None


def test_clean_dc2_quarter_all_attributes_pass() -> None:
    ans = build_quarter_gold_answer(_plan_engagement(), "DC-2", "Q1")
    assert ans.defect == "none"
    assert ans.expected_attribute_results == {
        "DC-2.A": "pass",
        "DC-2.B": "pass",
        "DC-2.C": "pass",
        "DC-2.D": "pass",
    }
    assert ans.expected_quarter_verdict == "Effective"
    assert ans.expected_cross_file_contradiction is None


# ── Defect → failing attribute mapping ───────────────────────────────


def test_dc9_figure_mismatch_fails_attribute_c() -> None:
    ans = build_quarter_gold_answer(_plan_engagement(), "DC-9", "Q3")
    assert ans.expected_attribute_results["DC-9.C"] == "fail"
    # All other attributes pass
    for letter in "ABDEF":
        assert ans.expected_attribute_results[f"DC-9.{letter}"] == "pass"
    assert ans.expected_quarter_verdict == "Not effective"


def test_dc9_rate_change_without_amendment_fails_attribute_d() -> None:
    ans = build_quarter_gold_answer(_plan_engagement(), "DC-9", "Q4")
    assert ans.expected_attribute_results["DC-9.D"] == "fail"
    for letter in "ABCEF":
        assert ans.expected_attribute_results[f"DC-9.{letter}"] == "pass"


def test_dc9_rate_change_with_amendment_is_benign_all_pass() -> None:
    ans = build_quarter_gold_answer(_plan_engagement(), "DC-9", "Q2")
    assert ans.defect == "dc9_rate_change_with_amendment"
    for letter in "ABCDEF":
        assert ans.expected_attribute_results[f"DC-9.{letter}"] == "pass"
    assert ans.expected_quarter_verdict == "Effective"
    assert ans.expected_cross_file_contradiction is None


def test_dc2_variance_no_explanation_fails_attribute_b() -> None:
    ans = build_quarter_gold_answer(_plan_engagement(), "DC-2", "Q4")
    assert ans.expected_attribute_results["DC-2.B"] == "fail"
    for letter in "ACD":
        assert ans.expected_attribute_results[f"DC-2.{letter}"] == "pass"


def test_dc2_variance_explanation_inadequate_fails_attribute_c() -> None:
    ans = build_quarter_gold_answer(_plan_engagement(), "DC-2", "Q3")
    assert ans.expected_attribute_results["DC-2.C"] == "fail"
    for letter in "ABD":
        assert ans.expected_attribute_results[f"DC-2.{letter}"] == "pass"


# ── Cross-file contradiction ─────────────────────────────────────────


def test_dc9_figure_mismatch_has_contradiction_pointer() -> None:
    ans = build_quarter_gold_answer(_plan_engagement(), "DC-9", "Q3")
    cfc = ans.expected_cross_file_contradiction
    assert cfc is not None
    assert (
        "billing-fee claim" in cfc["description"].lower() or "billing" in cfc["description"].lower()
    )
    # Quarter template substitution
    assert "Q3" in cfc["toc_cell"]
    assert "dc9_Q3_ref.xlsx" in cfc["wp_cell"]


def test_dc2_variance_explanation_inadequate_has_contradiction_pointer() -> None:
    ans = build_quarter_gold_answer(_plan_engagement(), "DC-2", "Q3")
    cfc = ans.expected_cross_file_contradiction
    assert cfc is not None
    assert "Source tie" in cfc["description"] or "source tie" in cfc["wp_cell"]
    assert "Q3" in cfc["toc_cell"]
    assert "dc2_Q3_ref.xlsx" in cfc["wp_cell"]


def test_dc9_rate_change_without_amendment_has_contradiction_pointer() -> None:
    ans = build_quarter_gold_answer(_plan_engagement(), "DC-9", "Q4")
    cfc = ans.expected_cross_file_contradiction
    assert cfc is not None
    assert "amendment" in cfc["description"].lower()
    assert "Q4" in cfc["toc_cell"]


# ── build_all_gold_answers ──────────────────────────────────────────


def test_build_all_gold_answers_returns_eight() -> None:
    spec = _plan_engagement()
    answers = build_all_gold_answers(spec)
    assert len(answers) == 8


def test_build_all_gold_answers_covers_every_quarter_control_pair() -> None:
    spec = _plan_engagement()
    answers = build_all_gold_answers(spec)
    pairs = {(a.control_id, a.quarter) for a in answers}
    expected = {(c, q) for c in ("DC-2", "DC-9") for q in ("Q1", "Q2", "Q3", "Q4")}
    assert pairs == expected


def test_build_all_gold_answers_verdict_distribution_matches_plan() -> None:
    """§5 plan → Q3 and Q4 are "Not effective" for both controls;
    Q1 and Q2 are "Effective" (Q2 DC-9 is benign rate_change_with_amendment).
    """
    spec = _plan_engagement()
    answers = {(a.control_id, a.quarter): a for a in build_all_gold_answers(spec)}

    for control in ("DC-2", "DC-9"):
        assert answers[(control, "Q1")].expected_quarter_verdict == "Effective"
        assert answers[(control, "Q2")].expected_quarter_verdict == "Effective"
        assert answers[(control, "Q3")].expected_quarter_verdict == "Not effective"
        assert answers[(control, "Q4")].expected_quarter_verdict == "Not effective"


# ── Serialize / load roundtrip ───────────────────────────────────────


def test_engagement_gold_answer_json_roundtrip(tmp_path: Path) -> None:
    spec = _plan_engagement()
    original = build_quarter_gold_answer(spec, "DC-9", "Q3")

    text = engagement_gold_answer_to_json(original)
    path = tmp_path / "dc9_Q3.json"
    path.write_text(text)

    reloaded = load_engagement_gold_answer(path)
    assert reloaded == original
    assert isinstance(reloaded, EngagementGoldAnswer)


def test_json_output_is_sorted_and_indented() -> None:
    spec = _plan_engagement()
    ans = build_quarter_gold_answer(spec, "DC-9", "Q3")
    text = engagement_gold_answer_to_json(ans)
    # Sort-keys: control_id comes before defect alphabetically
    assert text.find('"control_id"') < text.find('"defect"')
    # Indent=2
    assert "\n  " in text


# ── Frozen + determinism ─────────────────────────────────────────────


def test_engagement_gold_answer_is_deterministic() -> None:
    spec = _plan_engagement()
    a1 = build_quarter_gold_answer(spec, "DC-9", "Q3")
    a2 = build_quarter_gold_answer(spec, "DC-9", "Q3")
    assert a1 == a2
