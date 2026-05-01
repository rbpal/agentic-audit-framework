"""Validation tests for `agentic_audit.models.evidence`.

Covers the contract Layer 1 must enforce before any record reaches
`audit_dev.silver.evidence`: 4-attribute length, unique attribute IDs
covering `{A,B,C,D}`, control_id consistency across the four checks,
and the per-field Literal/length constraints.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agentic_audit.models.evidence import (
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
        evidence_cell_refs=[f"DC9_WP!{attribute_id}1"],
        extracted_value=None,
    )


def _make_evidence(
    *,
    control_id: str = "DC-9",
    quarter: str = "Q1",
    attributes: list[AttributeCheck] | None = None,
) -> ExtractedEvidence:
    if attributes is None:
        attributes = [_make_check(a, control_id=control_id) for a in ("A", "B", "C", "D")]
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


# ---------- Happy path ----------------------------------------------------


def test_valid_extracted_evidence_passes() -> None:
    e = _make_evidence()
    assert e.control_id == "DC-9"
    assert [a.attribute_id for a in e.attributes] == ["A", "B", "C", "D"]
    assert e.run_id.startswith("01J")


def test_dc2_evidence_with_dc2_attributes_passes() -> None:
    attrs = [_make_check(a, control_id="DC-2") for a in ("A", "B", "C", "D")]
    e = _make_evidence(control_id="DC-2", quarter="Q3", attributes=attrs)
    assert e.control_id == "DC-2"
    assert all(a.control_id == "DC-2" for a in e.attributes)


# ---------- attribute count ----------------------------------------------


def test_three_attributes_rejected() -> None:
    attrs = [_make_check(a) for a in ("A", "B", "C")]
    with pytest.raises(ValidationError) as exc:
        _make_evidence(attributes=attrs)
    assert "at least 4" in str(exc.value).lower() or "min_length" in str(exc.value).lower()


def test_five_attributes_rejected() -> None:
    attrs = [_make_check(a) for a in ("A", "B", "C", "D")] + [_make_check("D")]
    with pytest.raises(ValidationError) as exc:
        _make_evidence(attributes=attrs)
    assert "at most 4" in str(exc.value).lower() or "max_length" in str(exc.value).lower()


# ---------- attribute ID coverage ----------------------------------------


def test_duplicate_attribute_id_rejected() -> None:
    attrs = [_make_check(a) for a in ("A", "B", "C", "C")]
    with pytest.raises(ValidationError) as exc:
        _make_evidence(attributes=attrs)
    assert "must cover A,B,C,D exactly" in str(exc.value)


def test_missing_d_with_e_rejected() -> None:
    # Can't actually construct attribute_id="E" — Literal blocks it.
    # Closest legal failure: A, B, C, A (duplicate). Test that here.
    attrs = [_make_check(a) for a in ("A", "B", "C", "A")]
    with pytest.raises(ValidationError):
        _make_evidence(attributes=attrs)


def test_attribute_id_e_rejected_at_attribute_check() -> None:
    with pytest.raises(ValidationError) as exc:
        AttributeCheck(
            control_id="DC-9",
            attribute_id="E",  # type: ignore[arg-type]
            status="pass",
            evidence_cell_refs=[],
        )
    assert "E" in str(exc.value)


# ---------- control_id consistency ---------------------------------------


def test_mixed_control_id_rejected() -> None:
    attrs = [
        _make_check("A", control_id="DC-9"),
        _make_check("B", control_id="DC-9"),
        _make_check("C", control_id="DC-2"),  # the odd one out
        _make_check("D", control_id="DC-9"),
    ]
    with pytest.raises(ValidationError) as exc:
        _make_evidence(control_id="DC-9", attributes=attrs)
    assert "control_id" in str(exc.value)
    assert "DC-2" in str(exc.value)


# ---------- Literal / length constraints ---------------------------------


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
    attrs = [_make_check(a) for a in ("A", "B", "C", "D")]
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


# ---------- defaults ------------------------------------------------------


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
