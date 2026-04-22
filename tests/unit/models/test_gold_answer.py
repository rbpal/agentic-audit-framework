"""Unit tests for GoldAnswer model + build_gold_answer + JSON round-trip."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agentic_audit.models.gold_answer import (
    GoldAnswer,
    build_gold_answer,
    gold_answer_to_json,
)
from agentic_audit.models.scenario import ControlId, PatternType, ScenarioSpec


def _make_spec(
    control_id: ControlId = "DC-9",
    pattern_type: PatternType = "signoff_with_tieout",
    quarter: str = "Q1",
    expected_outcome: str = "pass",
    exception_type: str = "none",
    seed: int = 42,
    scenario_id: str | None = None,
) -> ScenarioSpec:
    return ScenarioSpec(
        scenario_id=scenario_id or f"x_gold_test_{seed}",
        control_id=control_id,
        pattern_type=pattern_type,
        quarter=quarter,  # type: ignore[arg-type]
        expected_outcome=expected_outcome,  # type: ignore[arg-type]
        exception_type=exception_type,  # type: ignore[arg-type]
        seed=seed,
    )


# ── Model validity ───────────────────────────────────────────────────


def test_valid_pass_gold_answer_constructs() -> None:
    gold = GoldAnswer(
        scenario_id="q1_pass_dc9_01",
        control_id="DC-9",
        expected_outcome="pass",
        expected_per_attribute_result={
            "DC-9.A": "pass",
            "DC-9.B": "pass",
            "DC-9.C": "pass",
            "DC-9.D": "pass",
            "DC-9.E": "pass",
            "DC-9.F": "pass",
        },
        expected_final_verdict="Effective",
        expected_exception_narrative_keywords=[],
    )
    assert gold.expected_final_verdict == "Effective"
    assert all(v == "pass" for v in gold.expected_per_attribute_result.values())


def test_invalid_verdict_rejected() -> None:
    with pytest.raises(ValidationError):
        GoldAnswer(
            scenario_id="x_bad_verdict_01",
            control_id="DC-9",
            expected_outcome="pass",
            expected_per_attribute_result={"DC-9.A": "pass"},
            expected_final_verdict="Partially Effective",  # type: ignore[arg-type]
            expected_exception_narrative_keywords=[],
        )


def test_invalid_attribute_result_rejected() -> None:
    with pytest.raises(ValidationError):
        GoldAnswer(
            scenario_id="x_bad_attr_01",
            control_id="DC-9",
            expected_outcome="pass",
            expected_per_attribute_result={"DC-9.A": "maybe"},  # type: ignore[dict-item]
            expected_final_verdict="Effective",
            expected_exception_narrative_keywords=[],
        )


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        GoldAnswer(  # type: ignore[call-arg]
            scenario_id="x_extra_01",
            control_id="DC-9",
            expected_outcome="pass",
            expected_per_attribute_result={"DC-9.A": "pass"},
            expected_final_verdict="Effective",
            expected_exception_narrative_keywords=[],
            bogus="should fail",
        )


# ── build_gold_answer (deterministic derivation) ─────────────────────


def test_build_gold_pass_scenario_all_pass() -> None:
    spec = _make_spec(
        scenario_id="q1_pass_dc9_01",
        control_id="DC-9",
        pattern_type="signoff_with_tieout",
        expected_outcome="pass",
        exception_type="none",
    )
    gold = build_gold_answer(spec)
    assert gold.expected_final_verdict == "Effective"
    assert all(v == "pass" for v in gold.expected_per_attribute_result.values())
    assert gold.expected_exception_narrative_keywords == []


def test_build_gold_dc9_has_six_attributes() -> None:
    spec = _make_spec(control_id="DC-9", pattern_type="signoff_with_tieout")
    gold = build_gold_answer(spec)
    assert set(gold.expected_per_attribute_result.keys()) == {
        "DC-9.A",
        "DC-9.B",
        "DC-9.C",
        "DC-9.D",
        "DC-9.E",
        "DC-9.F",
    }


def test_build_gold_dc2_has_four_attributes() -> None:
    spec = _make_spec(control_id="DC-2", pattern_type="variance_detection")
    gold = build_gold_answer(spec)
    assert set(gold.expected_per_attribute_result.keys()) == {
        "DC-2.A",
        "DC-2.B",
        "DC-2.C",
        "DC-2.D",
    }


def test_build_gold_figure_mismatch_fails_attribute_d() -> None:
    """Consistency: fake_data.pick_exception_attribute('figure_mismatch') == 'D'."""
    spec = _make_spec(
        control_id="DC-9",
        pattern_type="signoff_with_tieout",
        expected_outcome="exception",
        exception_type="figure_mismatch",
    )
    gold = build_gold_answer(spec)
    assert gold.expected_final_verdict == "Not effective"
    assert gold.expected_per_attribute_result["DC-9.D"] == "fail"
    # All others pass
    for letter in "ABCEF":
        assert gold.expected_per_attribute_result[f"DC-9.{letter}"] == "pass"


def test_build_gold_signoff_missing_fails_attribute_b() -> None:
    spec = _make_spec(
        control_id="DC-9",
        pattern_type="signoff_with_tieout",
        expected_outcome="exception",
        exception_type="signoff_missing",
    )
    gold = build_gold_answer(spec)
    assert gold.expected_per_attribute_result["DC-9.B"] == "fail"


def test_build_gold_variance_no_explanation_fails_attribute_b() -> None:
    spec = _make_spec(
        control_id="DC-2",
        pattern_type="variance_detection",
        expected_outcome="exception",
        exception_type="variance_above_threshold_no_explanation",
    )
    gold = build_gold_answer(spec)
    assert gold.expected_per_attribute_result["DC-2.B"] == "fail"


def test_build_gold_narrative_keywords_populated_for_exceptions() -> None:
    spec = _make_spec(
        control_id="DC-9",
        pattern_type="signoff_with_tieout",
        expected_outcome="exception",
        exception_type="figure_mismatch",
    )
    gold = build_gold_answer(spec)
    assert "mismatch" in gold.expected_exception_narrative_keywords


def test_build_gold_narrative_keywords_empty_for_pass() -> None:
    spec = _make_spec(expected_outcome="pass", exception_type="none")
    gold = build_gold_answer(spec)
    assert gold.expected_exception_narrative_keywords == []


def test_build_gold_is_deterministic() -> None:
    spec = _make_spec(scenario_id="x_determinism_01", seed=42)
    g1 = build_gold_answer(spec)
    g2 = build_gold_answer(spec)
    assert g1 == g2


# ── JSON serialization round-trip ────────────────────────────────────


def test_gold_answer_json_round_trip() -> None:
    spec = _make_spec(
        scenario_id="q3_exception_dc9_fmm_01",
        control_id="DC-9",
        pattern_type="signoff_with_tieout",
        expected_outcome="exception",
        exception_type="figure_mismatch",
    )
    gold = build_gold_answer(spec)
    text = gold_answer_to_json(gold)

    # Parseable JSON
    parsed = json.loads(text)
    assert parsed["scenario_id"] == "q3_exception_dc9_fmm_01"
    assert parsed["expected_final_verdict"] == "Not effective"

    # Deserialize back to GoldAnswer
    reloaded = GoldAnswer.model_validate(parsed)
    assert reloaded == gold


def test_gold_answer_json_has_stable_key_order() -> None:
    """Same GoldAnswer must serialize to byte-identical JSON across calls."""
    spec = _make_spec(scenario_id="x_stable_order_01")
    gold = build_gold_answer(spec)
    text1 = gold_answer_to_json(gold)
    text2 = gold_answer_to_json(gold)
    assert text1 == text2
