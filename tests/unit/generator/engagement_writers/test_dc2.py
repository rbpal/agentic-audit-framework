"""Tests for ``render_dc2_quarter`` (Step 2, v2 corpus)."""

from __future__ import annotations

from agentic_audit.generator.engagement_writers.dc2 import render_dc2_quarter
from agentic_audit.models.engagement import (
    EngagementSpec,
    QuarterControlSpec,
)


def _engagement_with_dc2_defect(quarter: str, defect: str) -> EngagementSpec:
    quarters = tuple(
        QuarterControlSpec(  # type: ignore[arg-type]
            control_id=c,
            quarter=q,
            defect=defect if (c == "DC-2" and q == quarter) else "none",
        )
        for c in ("DC-2", "DC-9")
        for q in ("Q1", "Q2", "Q3", "Q4")
    )
    return EngagementSpec(
        entity_name="Alpha Pension Fund",
        year=2025,
        seed=2025,
        quarters=quarters,
    )


def _clean_engagement() -> EngagementSpec:
    return _engagement_with_dc2_defect(quarter="Q1", defect="none")


# ── Structure ────────────────────────────────────────────────────────


def test_sheet_is_named_dc2_variance() -> None:
    wb = render_dc2_quarter(_clean_engagement(), "Q1")
    assert wb.sheetnames == ["DC-2 Variance"]


def test_header_has_entity_quarter_threshold() -> None:
    wb = render_dc2_quarter(_clean_engagement(), "Q2")
    ws = wb.active
    assert ws.cell(row=1, column=2).value == "Alpha Pension Fund"
    assert ws.cell(row=2, column=2).value == "Q2 2025"
    assert ws.cell(row=3, column=2).value == 5.0


# ── Attribute A (upstream feed tie-out) ──────────────────────────────


def test_attribute_a_ties_when_no_defect() -> None:
    wb = render_dc2_quarter(_clean_engagement(), "Q1")
    ws = wb.active
    upstream = ws.cell(row=6, column=2).value
    workbook = ws.cell(row=7, column=2).value
    tie_flag = ws.cell(row=7, column=3).value
    assert upstream == workbook
    assert tie_flag == "Ties to feed: Yes"


def test_attribute_a_fails_under_variance_boundary_defect() -> None:
    eng = _engagement_with_dc2_defect("Q3", "dc2_variance_boundary")
    wb = render_dc2_quarter(eng, "Q3")
    ws = wb.active
    upstream = ws.cell(row=6, column=2).value
    workbook = ws.cell(row=7, column=2).value
    tie_flag = ws.cell(row=7, column=3).value
    assert upstream != workbook
    assert tie_flag == "Ties to feed: No"


# ── Variance table (attributes B + C) ────────────────────────────────


def test_variance_table_has_five_revenue_streams() -> None:
    wb = render_dc2_quarter(_clean_engagement(), "Q1")
    ws = wb.active
    names = [ws.cell(row=r, column=1).value for r in range(10, 15)]
    assert names == [
        "Management Fees",
        "Performance Fees",
        "Interest Income",
        "Dividend Income",
        "Other Income",
    ]


def test_variance_arithmetic_row_ties() -> None:
    """variance = current - prior; % = variance / prior × 100."""
    wb = render_dc2_quarter(_clean_engagement(), "Q3")
    ws = wb.active
    for r in range(10, 15):
        prior = ws.cell(row=r, column=2).value
        current = ws.cell(row=r, column=3).value
        variance = ws.cell(row=r, column=4).value
        pct = ws.cell(row=r, column=5).value
        assert variance == current - prior
        # pct rounded to 2dp; compare against unrounded with tolerance
        expected_pct = (variance / prior) * 100.0
        assert abs(pct - round(expected_pct, 2)) < 0.01


def test_clean_scenario_above_threshold_rows_have_explanation_and_tie() -> None:
    wb = render_dc2_quarter(_clean_engagement(), "Q1")
    ws = wb.active
    any_above = False
    for r in range(10, 15):
        if ws.cell(row=r, column=6).value == "Yes":
            any_above = True
            assert ws.cell(row=r, column=7).value  # non-empty explanation
            assert ws.cell(row=r, column=8).value == "Yes"  # source tie
    assert any_above, "corpus should have at least one above-threshold row to be meaningful"


def test_variance_no_explanation_defect_leaves_first_above_row_blank() -> None:
    eng = _engagement_with_dc2_defect("Q4", "dc2_variance_no_explanation")
    wb = render_dc2_quarter(eng, "Q4")
    ws = wb.active
    found_blank = False
    for r in range(10, 15):
        above_threshold = ws.cell(row=r, column=6).value == "Yes"
        explanation_blank = ws.cell(row=r, column=7).value == ""
        if above_threshold and explanation_blank and not found_blank:
            assert ws.cell(row=r, column=8).value == "No — no explanation provided"
            found_blank = True
    assert found_blank, "dc2_variance_no_explanation defect didn't produce a blank row"


def test_variance_explanation_inadequate_defect_has_text_but_source_tie_no() -> None:
    eng = _engagement_with_dc2_defect("Q3", "dc2_variance_explanation_inadequate")
    wb = render_dc2_quarter(eng, "Q3")
    ws = wb.active
    any_inadequate = False
    for r in range(10, 15):
        if ws.cell(row=r, column=6).value == "Yes":
            explanation = ws.cell(row=r, column=7).value
            source_tie = ws.cell(row=r, column=8).value
            assert explanation  # non-empty
            assert source_tie == "No"
            any_inadequate = True
    assert any_inadequate, (
        "dc2_variance_explanation_inadequate defect should produce explanation+'No' source tie"
    )


# ── Attribute D (reviewer sign-off) ──────────────────────────────────


def test_reviewer_signoff_populated_every_quarter() -> None:
    for q in ("Q1", "Q2", "Q3", "Q4"):
        wb = render_dc2_quarter(_clean_engagement(), q)
        value = wb.active.cell(row=17, column=2).value
        assert value is not None
        initials, date_str = value.split(" — ")
        assert len(initials) == 2
        assert date_str.startswith("2025-")


# ── Determinism ─────────────────────────────────────────────────────


def test_render_dc2_quarter_is_deterministic() -> None:
    eng = _clean_engagement()
    wb1 = render_dc2_quarter(eng, "Q2")
    wb2 = render_dc2_quarter(eng, "Q2")
    for r in range(1, 20):
        for c in range(1, 9):
            assert wb1.active.cell(row=r, column=c).value == wb2.active.cell(row=r, column=c).value


def test_different_quarters_differ() -> None:
    eng = _clean_engagement()
    wb_q1 = render_dc2_quarter(eng, "Q1")
    wb_q3 = render_dc2_quarter(eng, "Q3")
    assert wb_q1.active.cell(row=2, column=2).value != wb_q3.active.cell(row=2, column=2).value
