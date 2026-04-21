"""Unit tests for ScenarioSpec + load_manifest."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_audit.models.scenario import ScenarioSpec, load_manifest

# Valid-construction tests -------------------------------------------


def test_valid_pass_scenario_constructs() -> None:
    spec = ScenarioSpec(
        scenario_id="q1_pass_example_01",
        control_id="DC-9",
        pattern_type="signoff_with_tieout",
        quarter="Q1",
        expected_outcome="pass",
        exception_type="none",
        seed=42,
    )
    assert spec.scenario_id == "q1_pass_example_01"
    assert spec.control_id == "DC-9"
    assert spec.seed == 42


def test_valid_exception_scenario_constructs() -> None:
    spec = ScenarioSpec(
        scenario_id="q3_exception_example_01",
        control_id="DC-2",
        pattern_type="variance_detection",
        quarter="Q3",
        expected_outcome="exception",
        exception_type="variance_above_threshold_no_explanation",
        seed=43,
    )
    assert spec.expected_outcome == "exception"
    assert spec.exception_type != "none"


# Field-level rejections ---------------------------------------------


def test_invalid_quarter_rejected() -> None:
    with pytest.raises(ValidationError, match="quarter"):
        ScenarioSpec(
            scenario_id="x_invalid_quarter_01",
            control_id="DC-9",
            pattern_type="signoff_with_tieout",
            quarter="Q2",  # type: ignore[arg-type]
            expected_outcome="pass",
            exception_type="none",
            seed=0,
        )


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        ScenarioSpec(  # type: ignore[call-arg]
            scenario_id="x_extra_field_01",
            control_id="DC-9",
            pattern_type="signoff_with_tieout",
            quarter="Q1",
            expected_outcome="pass",
            exception_type="none",
            seed=0,
            bogus_field="should-reject",
        )


def test_negative_seed_rejected() -> None:
    with pytest.raises(ValidationError, match="seed"):
        ScenarioSpec(
            scenario_id="x_neg_seed_01",
            control_id="DC-9",
            pattern_type="signoff_with_tieout",
            quarter="Q1",
            expected_outcome="pass",
            exception_type="none",
            seed=-1,
        )


# Cross-field validators ---------------------------------------------


def test_control_pattern_mismatch_rejected() -> None:
    with pytest.raises(ValidationError, match="pattern_type"):
        ScenarioSpec(
            scenario_id="x_mismatch_01",
            control_id="DC-9",
            pattern_type="variance_detection",
            quarter="Q1",
            expected_outcome="pass",
            exception_type="none",
            seed=0,
        )


def test_pass_outcome_requires_exception_none() -> None:
    with pytest.raises(ValidationError, match="exception_type"):
        ScenarioSpec(
            scenario_id="x_pass_exc_01",
            control_id="DC-9",
            pattern_type="signoff_with_tieout",
            quarter="Q1",
            expected_outcome="pass",
            exception_type="signoff_missing",
            seed=0,
        )


def test_exception_outcome_requires_specific_type() -> None:
    with pytest.raises(ValidationError, match="exception"):
        ScenarioSpec(
            scenario_id="x_exc_none_01",
            control_id="DC-9",
            pattern_type="signoff_with_tieout",
            quarter="Q1",
            expected_outcome="exception",
            exception_type="none",
            seed=0,
        )


# Frozen enforcement -------------------------------------------------


def test_model_is_frozen() -> None:
    spec = ScenarioSpec(
        scenario_id="q1_pass_frozen_01",
        control_id="DC-9",
        pattern_type="signoff_with_tieout",
        quarter="Q1",
        expected_outcome="pass",
        exception_type="none",
        seed=42,
    )
    with pytest.raises(ValidationError):
        spec.seed = 99  # type: ignore[misc]


# YAML manifest round-trip -------------------------------------------


def test_load_manifest_parses_seed_manifest() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    manifest = repo_root / "eval" / "gold_scenarios" / "manifest.yaml"
    specs = load_manifest(manifest)
    assert len(specs) >= 2
    assert all(isinstance(s, ScenarioSpec) for s in specs)
    assert all(isinstance(s.seed, int) for s in specs)
