"""Validation tests for `agentic_audit.models.evidence`.

Covers the contract Layer 1 must enforce before any record reaches
`audit_dev.silver.evidence`:

- DC-2 carries 4 attributes (A, B, C, D).
- DC-9 carries 6 attributes (A, B, C, D, E, F).
- Per-control coverage is exact — no gaps, no extras, no duplicates.
- Every nested AttributeCheck.control_id matches the parent's.
- Per-field Literal / length constraints are enforced.

Source of truth for attribute counts: the engagement TOC files at
``eval/gold_scenarios/tocs/*.json``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agentic_audit.models.evidence import (
    ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)

UTC_TS = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)


def _make_check(
    attribute_id: str,
    *,
    control_id: str = "DC-9",
    status: str = "pass",
) -> AttributeCheck:
    return AttributeCheck(
        control_id=control_id,  # type: ignore[arg-type]
        attribute_id=attribute_id,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        evidence_cell_refs=[f"{control_id.replace('-', '')}_WP!{attribute_id}1"],
        extracted_value=None,
    )


def _make_evidence(
    *,
    control_id: str = "DC-9",
    quarter: str = "Q1",
    attributes: list[AttributeCheck] | None = None,
) -> ExtractedEvidence:
    if attributes is None:
        ids = ATTRIBUTES_PER_CONTROL[control_id]
        attributes = [_make_check(a, control_id=control_id) for a in ids]
    return ExtractedEvidence(
        engagement_id="alpha-pension-fund-2025",
        control_id=control_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        run_id="01J0F7M5XQXM2QYAY8X8X8X8X8",
        extraction_timestamp=UTC_TS,
        preparer=SignOff(initials="AB", role="preparer", date=UTC_TS),
        reviewer=SignOff(initials="CD", role="reviewer", date=UTC_TS),
        attributes=attributes,
        source_bronze_file_hash="a" * 64,
    )


# ---------- coverage map ---------------------------------------------------


def test_attributes_per_control_map_matches_corpus() -> None:
    """Smoke test the constant against the documented spec.

    If the corpus changes (e.g. DC-2 grows an attribute E), this test
    becomes the failing breadcrumb that points at every other thing
    that needs updating.
    """
    assert ATTRIBUTES_PER_CONTROL == {
        "DC-2": ["A", "B", "C", "D"],
        "DC-9": ["A", "B", "C", "D", "E", "F"],
    }


# ---------- happy paths ----------------------------------------------------


def test_dc2_with_four_attributes_passes() -> None:
    e = _make_evidence(control_id="DC-2")
    assert e.control_id == "DC-2"
    assert [a.attribute_id for a in e.attributes] == ["A", "B", "C", "D"]


def test_dc9_with_six_attributes_passes() -> None:
    e = _make_evidence(control_id="DC-9")
    assert e.control_id == "DC-9"
    assert [a.attribute_id for a in e.attributes] == ["A", "B", "C", "D", "E", "F"]


def test_dc9_q3_pass_case_with_all_six() -> None:
    attrs = [_make_check(a, control_id="DC-9") for a in ("A", "B", "C", "D", "E", "F")]
    e = _make_evidence(control_id="DC-9", quarter="Q3", attributes=attrs)
    assert e.quarter == "Q3"
    assert len(e.attributes) == 6


# ---------- per-control coverage -------------------------------------------


def test_dc2_with_only_three_attributes_rejected() -> None:
    attrs = [_make_check(a, control_id="DC-2") for a in ("A", "B", "C")]
    with pytest.raises(ValidationError) as exc:
        _make_evidence(control_id="DC-2", attributes=attrs)
    # The list min_length=4 fires first (Pydantic field-level), so the
    # message is the framework one, not our custom validator's.
    assert "at least 4" in str(exc.value).lower() or "min_length" in str(exc.value).lower()


def test_dc2_with_six_attributes_a_through_f_rejected() -> None:
    """DC-2 must have exactly A,B,C,D — extending it to F is a contract bug."""
    attrs = [_make_check(a, control_id="DC-2") for a in ("A", "B", "C", "D", "E", "F")]
    with pytest.raises(ValidationError) as exc:
        _make_evidence(control_id="DC-2", attributes=attrs)
    assert "DC-2 requires attributes" in str(exc.value)
    assert "['A', 'B', 'C', 'D']" in str(exc.value)


def test_dc9_with_only_four_attributes_rejected() -> None:
    """DC-9 needs A-F. Just A-D is missing E and F."""
    attrs = [_make_check(a, control_id="DC-9") for a in ("A", "B", "C", "D")]
    with pytest.raises(ValidationError) as exc:
        _make_evidence(control_id="DC-9", attributes=attrs)
    assert "DC-9 requires attributes" in str(exc.value)
    assert "['A', 'B', 'C', 'D', 'E', 'F']" in str(exc.value)


def test_dc9_with_five_attributes_a_b_c_d_e_rejected() -> None:
    """Missing F is still a contract violation."""
    attrs = [_make_check(a, control_id="DC-9") for a in ("A", "B", "C", "D", "E")]
    with pytest.raises(ValidationError) as exc:
        _make_evidence(control_id="DC-9", attributes=attrs)
    assert "DC-9 requires attributes" in str(exc.value)


def test_dc2_with_a_b_c_a_rejected_duplicate() -> None:
    attrs = [_make_check(a, control_id="DC-2") for a in ("A", "B", "C", "A")]
    with pytest.raises(ValidationError) as exc:
        _make_evidence(control_id="DC-2", attributes=attrs)
    assert "DC-2 requires attributes" in str(exc.value)


def test_dc9_with_seven_entries_extra_a_rejected() -> None:
    """7 entries (A-F + duplicate A) breaks max_length=6."""
    attrs = [_make_check(a, control_id="DC-9") for a in ("A", "B", "C", "D", "E", "F", "A")]
    with pytest.raises(ValidationError) as exc:
        _make_evidence(control_id="DC-9", attributes=attrs)
    assert "at most 6" in str(exc.value).lower() or "max_length" in str(exc.value).lower()


# ---------- control_id consistency ----------------------------------------


def test_dc9_with_one_dc2_attribute_rejected() -> None:
    attrs = [
        _make_check("A", control_id="DC-9"),
        _make_check("B", control_id="DC-9"),
        _make_check("C", control_id="DC-2"),  # the odd one out
        _make_check("D", control_id="DC-9"),
        _make_check("E", control_id="DC-9"),
        _make_check("F", control_id="DC-9"),
    ]
    with pytest.raises(ValidationError) as exc:
        _make_evidence(control_id="DC-9", attributes=attrs)
    assert "control_id=DC-2" in str(exc.value)


def test_dc2_with_one_dc9_attribute_rejected() -> None:
    attrs = [
        _make_check("A", control_id="DC-2"),
        _make_check("B", control_id="DC-2"),
        _make_check("C", control_id="DC-9"),  # the odd one out
        _make_check("D", control_id="DC-2"),
    ]
    with pytest.raises(ValidationError) as exc:
        _make_evidence(control_id="DC-2", attributes=attrs)
    assert "control_id=DC-9" in str(exc.value)


# ---------- Literal / length constraints ----------------------------------


def test_attribute_id_g_rejected_at_attribute_check() -> None:
    """G is outside the AttributeId Literal."""
    with pytest.raises(ValidationError) as exc:
        AttributeCheck(
            control_id="DC-9",
            attribute_id="G",  # type: ignore[arg-type]
            status="pass",
            evidence_cell_refs=[],
        )
    assert "G" in str(exc.value)


def test_attribute_id_e_now_accepted_at_attribute_check() -> None:
    """E used to be rejected (was wrongly outside Literal); now valid for DC-9."""
    c = AttributeCheck(
        control_id="DC-9",
        attribute_id="E",
        status="pass",
        evidence_cell_refs=["DC9_WP!E1"],
    )
    assert c.attribute_id == "E"


def test_quarter_q5_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        _make_evidence(quarter="Q5")
    assert "Q5" in str(exc.value) or "Q1" in str(exc.value)


def test_status_error_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        AttributeCheck(
            control_id="DC-9",
            attribute_id="A",
            status="error",  # type: ignore[arg-type]
            evidence_cell_refs=[],
        )
    assert "error" in str(exc.value)


def test_signoff_initials_one_char_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        SignOff(initials="A", role="preparer", date=UTC_TS)
    assert "at least 2" in str(exc.value).lower() or "min_length" in str(exc.value).lower()


def test_signoff_initials_five_chars_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        SignOff(initials="ABCDE", role="preparer", date=UTC_TS)
    assert "at most 4" in str(exc.value).lower() or "max_length" in str(exc.value).lower()


def test_empty_engagement_id_rejected() -> None:
    attrs = [_make_check(a, control_id="DC-9") for a in ATTRIBUTES_PER_CONTROL["DC-9"]]
    with pytest.raises(ValidationError):
        ExtractedEvidence(
            engagement_id="",
            control_id="DC-9",
            quarter="Q1",
            run_id="01J0F7M5XQXM2QYAY8X8X8X8X8",
            extraction_timestamp=UTC_TS,
            preparer=SignOff(initials="AB", role="preparer", date=UTC_TS),
            reviewer=SignOff(initials="CD", role="reviewer", date=UTC_TS),
            attributes=attrs,
            source_bronze_file_hash="a" * 64,
        )


# ---------- defaults -------------------------------------------------------


def test_attribute_check_default_evidence_cell_refs_empty_list() -> None:
    c = AttributeCheck(
        control_id="DC-9",
        attribute_id="A",
        status="n/a",
    )
    assert c.evidence_cell_refs == []
    assert c.extracted_value is None
    assert c.notes is None


def test_status_n_a_accepted() -> None:
    c = _make_check("A", status="n/a")
    assert c.status == "n/a"
