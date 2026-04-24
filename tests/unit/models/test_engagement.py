"""Tests for the v2 ``EngagementSpec`` schema (Step 2 Task 01).

Pure schema validation — no writers, no corpus, no CLI. Writers that
consume ``EngagementSpec`` arrive in Tasks 02–05.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_audit.models.engagement import (
    EngagementSpec,
    QuarterControlSpec,
    load_engagement,
    quarter_control,
)

# ── Helpers ──────────────────────────────────────────────────────────


_ALL_QUARTER_CONTROLS = tuple(
    QuarterControlSpec(control_id=c, quarter=q, defect="none")  # type: ignore[arg-type]
    for c in ("DC-2", "DC-9")
    for q in ("Q1", "Q2", "Q3", "Q4")
)


def _valid_engagement(**overrides: object) -> EngagementSpec:
    defaults: dict[str, object] = {
        "entity_name": "Alpha Pension Fund",
        "year": 2025,
        "seed": 2025,
        "quarters": _ALL_QUARTER_CONTROLS,
    }
    defaults.update(overrides)
    return EngagementSpec(**defaults)  # type: ignore[arg-type]


# ── QuarterControlSpec ───────────────────────────────────────────────


def test_quarter_control_spec_defaults_to_clean_defect() -> None:
    qc = QuarterControlSpec(control_id="DC-9", quarter="Q1")
    assert qc.defect == "none"


def test_quarter_control_spec_accepts_dc9_defect_on_dc9() -> None:
    qc = QuarterControlSpec(control_id="DC-9", quarter="Q3", defect="dc9_figure_mismatch")
    assert qc.defect == "dc9_figure_mismatch"


def test_quarter_control_spec_accepts_dc2_defect_on_dc2() -> None:
    qc = QuarterControlSpec(control_id="DC-2", quarter="Q4", defect="dc2_variance_no_explanation")
    assert qc.defect == "dc2_variance_no_explanation"


def test_quarter_control_spec_rejects_dc9_defect_on_dc2() -> None:
    with pytest.raises(ValidationError, match="only valid on control_id='DC-9'"):
        QuarterControlSpec(control_id="DC-2", quarter="Q3", defect="dc9_figure_mismatch")


def test_quarter_control_spec_rejects_dc2_defect_on_dc9() -> None:
    with pytest.raises(ValidationError, match="only valid on control_id='DC-2'"):
        QuarterControlSpec(control_id="DC-9", quarter="Q4", defect="dc2_variance_boundary")


def test_quarter_control_spec_rejects_unknown_quarter() -> None:
    with pytest.raises(ValidationError):
        QuarterControlSpec(control_id="DC-9", quarter="Q5")  # type: ignore[arg-type]


def test_quarter_control_spec_rejects_unknown_control() -> None:
    with pytest.raises(ValidationError):
        QuarterControlSpec(control_id="DC-99", quarter="Q1")  # type: ignore[arg-type]


def test_quarter_control_spec_is_frozen() -> None:
    qc = QuarterControlSpec(control_id="DC-9", quarter="Q1")
    with pytest.raises(ValidationError):
        qc.defect = "dc9_figure_mismatch"  # type: ignore[misc]


def test_quarter_control_spec_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        QuarterControlSpec(
            control_id="DC-9",
            quarter="Q1",
            defect="none",
            unknown_field="oops",  # type: ignore[call-arg]
        )


# ── EngagementSpec — happy path ──────────────────────────────────────


def test_engagement_spec_accepts_all_eight_quarter_control_combinations() -> None:
    spec = _valid_engagement()
    assert len(spec.quarters) == 8
    assert spec.entity_name == "Alpha Pension Fund"
    assert spec.year == 2025


def test_engagement_spec_accepts_each_defect_from_plan() -> None:
    """Verify the §5 exception placement from step_02_corpus_v2.md parses."""
    quarters = (
        # Q1 clean
        QuarterControlSpec(control_id="DC-2", quarter="Q1", defect="none"),
        QuarterControlSpec(control_id="DC-9", quarter="Q1", defect="none"),
        # Q2 benign rate change
        QuarterControlSpec(control_id="DC-2", quarter="Q2", defect="none"),
        QuarterControlSpec(
            control_id="DC-9", quarter="Q2", defect="dc9_rate_change_with_amendment"
        ),
        # Q3 two simultaneous defects
        QuarterControlSpec(
            control_id="DC-2", quarter="Q3", defect="dc2_variance_explanation_inadequate"
        ),
        QuarterControlSpec(control_id="DC-9", quarter="Q3", defect="dc9_figure_mismatch"),
        # Q4 different two defects
        QuarterControlSpec(control_id="DC-2", quarter="Q4", defect="dc2_variance_no_explanation"),
        QuarterControlSpec(
            control_id="DC-9", quarter="Q4", defect="dc9_rate_change_without_amendment"
        ),
    )
    spec = _valid_engagement(quarters=quarters)
    assert len(spec.quarters) == 8


# ── EngagementSpec — coverage validator ──────────────────────────────


def test_engagement_spec_rejects_missing_combination() -> None:
    """7 entries instead of 8 — missing (DC-9, Q4)."""
    quarters = tuple(
        qc for qc in _ALL_QUARTER_CONTROLS if not (qc.control_id == "DC-9" and qc.quarter == "Q4")
    )
    with pytest.raises(ValidationError, match=r"missing: \[\('DC-9', 'Q4'\)\]"):
        _valid_engagement(quarters=quarters)


def test_engagement_spec_rejects_duplicate_combination() -> None:
    """Two entries for (DC-9, Q1) — duplicates."""
    quarters = (
        QuarterControlSpec(control_id="DC-9", quarter="Q1"),
        QuarterControlSpec(control_id="DC-9", quarter="Q1", defect="dc9_figure_mismatch"),
        *(
            qc
            for qc in _ALL_QUARTER_CONTROLS
            if not (qc.control_id == "DC-9" and qc.quarter == "Q1")
        ),
    )
    with pytest.raises(ValidationError, match=r"duplicate.*DC-9.*Q1"):
        _valid_engagement(quarters=quarters)


def test_engagement_spec_rejects_empty_quarters() -> None:
    with pytest.raises(ValidationError, match="missing"):
        _valid_engagement(quarters=())


# ── EngagementSpec — field validation ────────────────────────────────


def test_engagement_spec_rejects_empty_entity_name() -> None:
    with pytest.raises(ValidationError):
        _valid_engagement(entity_name="")


def test_engagement_spec_rejects_year_out_of_range() -> None:
    with pytest.raises(ValidationError):
        _valid_engagement(year=1999)
    with pytest.raises(ValidationError):
        _valid_engagement(year=2101)


def test_engagement_spec_rejects_negative_seed() -> None:
    with pytest.raises(ValidationError):
        _valid_engagement(seed=-1)


def test_engagement_spec_is_frozen() -> None:
    spec = _valid_engagement()
    with pytest.raises(ValidationError):
        spec.entity_name = "Beta Fund"  # type: ignore[misc]


def test_engagement_spec_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        EngagementSpec(
            entity_name="Alpha Pension Fund",
            year=2025,
            seed=2025,
            quarters=_ALL_QUARTER_CONTROLS,
            audit_lead="someone",  # type: ignore[call-arg]
        )


# ── YAML list → tuple coercion ───────────────────────────────────────


def test_engagement_spec_accepts_list_from_yaml() -> None:
    """pydantic strict mode rejects list where tuple is declared; the
    before-validator coerces list → tuple at the boundary.
    """
    list_input = list(_ALL_QUARTER_CONTROLS)
    spec = _valid_engagement(quarters=list_input)  # type: ignore[arg-type]
    assert isinstance(spec.quarters, tuple)
    assert len(spec.quarters) == 8


# ── Loader + lookup helpers ──────────────────────────────────────────


def test_load_engagement_from_yaml_file(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """
engagement:
  entity_name: Alpha Pension Fund
  year: 2025
  seed: 2025
  quarters:
    - control_id: DC-2
      quarter: Q1
      defect: none
    - control_id: DC-2
      quarter: Q2
      defect: none
    - control_id: DC-2
      quarter: Q3
      defect: dc2_variance_explanation_inadequate
    - control_id: DC-2
      quarter: Q4
      defect: dc2_variance_no_explanation
    - control_id: DC-9
      quarter: Q1
      defect: none
    - control_id: DC-9
      quarter: Q2
      defect: dc9_rate_change_with_amendment
    - control_id: DC-9
      quarter: Q3
      defect: dc9_figure_mismatch
    - control_id: DC-9
      quarter: Q4
      defect: dc9_rate_change_without_amendment
""".strip()
    )
    spec = load_engagement(manifest)
    assert spec.entity_name == "Alpha Pension Fund"
    assert spec.year == 2025
    assert len(spec.quarters) == 8


def test_quarter_control_lookup_returns_right_entry() -> None:
    spec = _valid_engagement()
    qc = quarter_control(spec, control_id="DC-9", quarter="Q3")
    assert qc.control_id == "DC-9"
    assert qc.quarter == "Q3"


def test_quarter_control_lookup_finds_defect_if_set() -> None:
    """Custom engagement with a defect on (DC-9, Q3)."""
    quarters = tuple(
        QuarterControlSpec(control_id=c, quarter=q, defect="dc9_figure_mismatch")  # type: ignore[arg-type]
        if (c, q) == ("DC-9", "Q3")
        else QuarterControlSpec(control_id=c, quarter=q, defect="none")  # type: ignore[arg-type]
        for c in ("DC-2", "DC-9")
        for q in ("Q1", "Q2", "Q3", "Q4")
    )
    spec = _valid_engagement(quarters=quarters)
    qc = quarter_control(spec, control_id="DC-9", quarter="Q3")
    assert qc.defect == "dc9_figure_mismatch"
