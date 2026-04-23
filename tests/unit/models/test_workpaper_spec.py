"""Tests for ``WorkpaperSpec`` + ``ScenarioSpec.workpapers`` field (Task 11).

Schema-only validation. No generator integration, no manifest YAML use yet
— Task 12+ will wire the field into file emission.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentic_audit.models import ScenarioSpec, WorkpaperSpec

# ── WorkpaperSpec itself ─────────────────────────────────────────────


def test_workpaper_spec_creates_with_minimal_fields() -> None:
    wp = WorkpaperSpec(
        type="billing_calculation",
        filename="billing_calc.xlsx",
        toc_reference_code="DC-5.7",
    )
    assert wp.type == "billing_calculation"
    assert wp.filename == "billing_calc.xlsx"
    assert wp.toc_reference_code == "DC-5.7"


def test_workpaper_spec_is_frozen() -> None:
    wp = WorkpaperSpec(type="variance_analysis", filename="v.xlsx", toc_reference_code="DC-10.2")
    with pytest.raises(ValidationError):
        wp.filename = "other.xlsx"  # type: ignore[misc]


def test_workpaper_spec_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        WorkpaperSpec(
            type="budget_spreadsheet",  # type: ignore[arg-type]
            filename="b.xlsx",
            toc_reference_code="X-1.1",
        )


def test_workpaper_spec_rejects_empty_filename() -> None:
    with pytest.raises(ValidationError):
        WorkpaperSpec(type="billing_calculation", filename="", toc_reference_code="DC-5.7")


def test_workpaper_spec_rejects_empty_reference_code() -> None:
    with pytest.raises(ValidationError):
        WorkpaperSpec(type="billing_calculation", filename="b.xlsx", toc_reference_code="")


def test_workpaper_spec_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        WorkpaperSpec(
            type="billing_calculation",
            filename="b.xlsx",
            toc_reference_code="DC-5.7",
            unexpected_field="oops",  # type: ignore[call-arg]
        )


# ── ScenarioSpec.workpapers default + integration ──────────────────────


def _base_scenario_kwargs() -> dict[str, object]:
    return dict(
        scenario_id="q1_pass_dc9_01",
        control_id="DC-9",
        pattern_type="signoff_with_tieout",
        quarter="Q1",
        expected_outcome="pass",
        exception_type="none",
        seed=42,
    )


def test_scenario_spec_workpapers_defaults_to_empty_tuple() -> None:
    """Pre-Task-11 call sites that construct ScenarioSpec without workpapers
    must keep working.
    """
    spec = ScenarioSpec(**_base_scenario_kwargs())  # type: ignore[arg-type]
    assert spec.workpapers == ()


def test_scenario_spec_accepts_workpapers_field() -> None:
    wp1 = WorkpaperSpec(
        type="billing_calculation",
        filename="billing_calc.xlsx",
        toc_reference_code="DC-5.7",
    )
    wp2 = WorkpaperSpec(
        type="governing_document_amendment",
        filename="governing_doc.xlsx",
        toc_reference_code="1.03",
    )
    spec = ScenarioSpec(**_base_scenario_kwargs(), workpapers=(wp1, wp2))  # type: ignore[arg-type]
    assert len(spec.workpapers) == 2
    assert spec.workpapers[0].type == "billing_calculation"
    assert spec.workpapers[1].type == "governing_document_amendment"


def test_scenario_spec_rejects_duplicate_filenames() -> None:
    wp1 = WorkpaperSpec(type="billing_calculation", filename="b.xlsx", toc_reference_code="DC-5.7")
    wp2 = WorkpaperSpec(
        type="governing_document_amendment",
        filename="b.xlsx",
        toc_reference_code="1.03",
    )
    with pytest.raises(ValidationError, match="filenames must be unique"):
        ScenarioSpec(**_base_scenario_kwargs(), workpapers=(wp1, wp2))  # type: ignore[arg-type]


def test_scenario_spec_rejects_duplicate_reference_codes() -> None:
    wp1 = WorkpaperSpec(
        type="billing_calculation",
        filename="billing.xlsx",
        toc_reference_code="DC-5.7",
    )
    wp2 = WorkpaperSpec(
        type="governing_document_amendment",
        filename="governing.xlsx",
        toc_reference_code="DC-5.7",
    )
    with pytest.raises(ValidationError, match="toc_reference_code values must be unique"):
        ScenarioSpec(**_base_scenario_kwargs(), workpapers=(wp1, wp2))  # type: ignore[arg-type]


def test_scenario_spec_accepts_single_workpaper() -> None:
    wp = WorkpaperSpec(
        type="billing_calculation",
        filename="billing_calc.xlsx",
        toc_reference_code="DC-5.7",
    )
    spec = ScenarioSpec(**_base_scenario_kwargs(), workpapers=(wp,))  # type: ignore[arg-type]
    assert spec.workpapers == (wp,)


def test_scenario_spec_with_workpapers_is_frozen() -> None:
    """workpapers tuple is immutable via the frozen config."""
    wp = WorkpaperSpec(type="billing_calculation", filename="b.xlsx", toc_reference_code="DC-5.7")
    spec = ScenarioSpec(**_base_scenario_kwargs(), workpapers=(wp,))  # type: ignore[arg-type]
    new_wp = WorkpaperSpec(
        type="variance_analysis", filename="v.xlsx", toc_reference_code="DC-10.2"
    )
    with pytest.raises(ValidationError):
        spec.workpapers = (new_wp,)  # type: ignore[misc]
