"""Tests for ``render_dc9_quarter`` (Step 2, v2 corpus).

Writer-level contract:

* Every DC-9 attribute (A–F) has at least one cell on the sheet.
* Sign-offs populated for every quarter (A + B always pass).
* Billing calc rows tie arithmetically (fee = assets × rate).
* Asset roll-forward ties by construction (opening + additions − retirements = closing).
* Ownership percentages sum to 100 %.
* Rate-change section reflects quarter-to-quarter progression per §5 plan.
* Amendment cell reflects the declared defect.
* Determinism: same seed → same workbook bytes across runs.
"""

from __future__ import annotations

from agentic_audit.generator.engagement_writers.dc9 import render_dc9_quarter
from agentic_audit.models.engagement import (
    EngagementSpec,
    QuarterControlSpec,
)


def _engagement_with_dc9_defect(quarter: str, defect: str) -> EngagementSpec:
    """Build an engagement where (DC-9, quarter) carries the given defect;
    every other (control, quarter) is clean.
    """
    quarters = tuple(
        QuarterControlSpec(  # type: ignore[arg-type]
            control_id=c,
            quarter=q,
            defect=defect if (c == "DC-9" and q == quarter) else "none",
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
    return _engagement_with_dc9_defect(quarter="Q1", defect="none")


# ── structure ────────────────────────────────────────────────────────


def test_sheet_is_named_dc9_billing() -> None:
    wb = render_dc9_quarter(_clean_engagement(), "Q1")
    assert wb.sheetnames == ["DC-9 Billing"]


def test_header_rows_populate_entity_quarter_date() -> None:
    wb = render_dc9_quarter(_clean_engagement(), "Q3")
    ws = wb.active
    assert ws.cell(row=1, column=2).value == "Alpha Pension Fund"
    assert ws.cell(row=2, column=2).value == "Q3 2025"
    assert ws.cell(row=3, column=2).value == "2025-09-30"


# ── Attribute A (preparer sign-off) ──────────────────────────────────


def test_preparer_signoff_populated_every_quarter() -> None:
    for q in ("Q1", "Q2", "Q3", "Q4"):
        wb = render_dc9_quarter(_clean_engagement(), q)
        value = wb.active.cell(row=4, column=2).value
        assert value is not None
        # Format: "XX — YYYY-MM-DD"
        initials, date_str = value.split(" — ")
        assert len(initials) == 2
        assert date_str.startswith("2025-")


# ── Attribute B (reviewer sign-off) ──────────────────────────────────


def test_reviewer_signoff_populated_every_quarter() -> None:
    for q in ("Q1", "Q2", "Q3", "Q4"):
        wb = render_dc9_quarter(_clean_engagement(), q)
        value = wb.active.cell(row=5, column=2).value
        assert value is not None
        initials, date_str = value.split(" — ")
        assert len(initials) == 2
        assert date_str.startswith("2025-")


def test_preparer_and_reviewer_are_different_initials() -> None:
    wb = render_dc9_quarter(_clean_engagement(), "Q1")
    prep = wb.active.cell(row=4, column=2).value
    rev = wb.active.cell(row=5, column=2).value
    # Different rngs → almost always different draws. Assert for Q1.
    assert prep != rev


# ── Attribute C (billing calculation arithmetic) ─────────────────────


def test_billing_fee_equals_assets_times_rate() -> None:
    for q in ("Q1", "Q2", "Q3", "Q4"):
        wb = render_dc9_quarter(_clean_engagement(), q)
        ws = wb.active
        asset_value = ws.cell(row=8, column=2).value
        rate_str = ws.cell(row=9, column=2).value
        fee = ws.cell(row=10, column=2).value

        rate_decimal = float(rate_str.rstrip("%")) / 100.0
        assert fee == int(asset_value * rate_decimal)


def test_billing_rate_progression_across_quarters() -> None:
    """§5 plan: Q1=0.25%, Q2=0.50%, Q3=0.50%, Q4=0.75%."""
    eng = _clean_engagement()
    expected = {"Q1": "0.25%", "Q2": "0.50%", "Q3": "0.50%", "Q4": "0.75%"}
    for q, want in expected.items():
        wb = render_dc9_quarter(eng, q)  # type: ignore[arg-type]
        assert wb.active.cell(row=9, column=2).value == want


# ── Attribute D (rate change + amendment reference) ────────────────


def test_q1_rate_change_section_shows_first_period_marker() -> None:
    wb = render_dc9_quarter(_clean_engagement(), "Q1")
    ws = wb.active
    assert ws.cell(row=13, column=2).value == "N/A — first period"
    assert ws.cell(row=15, column=2).value == "N/A — no rate change"
    assert ws.cell(row=16, column=2).value == "N/A — no rate change"


def test_q2_with_amendment_rate_change_section_shows_amendment_on_file() -> None:
    eng = _engagement_with_dc9_defect("Q2", "dc9_rate_change_with_amendment")
    wb = render_dc9_quarter(eng, "Q2")
    ws = wb.active
    assert ws.cell(row=13, column=2).value == "0.25%"  # prior = Q1
    assert ws.cell(row=14, column=2).value == "0.50%"
    assert "Amendment on file" in ws.cell(row=16, column=2).value


def test_q3_no_rate_change_shows_na_for_amendment() -> None:
    """Q3 rate matches Q2 (both 0.50%), so no change → no amendment row active."""
    wb = render_dc9_quarter(_clean_engagement(), "Q3")
    ws = wb.active
    assert ws.cell(row=13, column=2).value == "0.50%"
    assert ws.cell(row=14, column=2).value == "0.50%"
    assert ws.cell(row=15, column=2).value == "N/A — no rate change"
    assert ws.cell(row=16, column=2).value == "N/A — no rate change"


def test_q4_without_amendment_shows_defect_marker() -> None:
    eng = _engagement_with_dc9_defect("Q4", "dc9_rate_change_without_amendment")
    wb = render_dc9_quarter(eng, "Q4")
    ws = wb.active
    assert ws.cell(row=13, column=2).value == "0.50%"  # prior = Q3
    assert ws.cell(row=14, column=2).value == "0.75%"
    assert ws.cell(row=16, column=2).value == "NO AMENDMENT FILED"


# ── Attribute E (asset roll-forward) ─────────────────────────────────


def test_asset_roll_forward_ties_by_construction() -> None:
    """opening + additions - retirements = closing (= asset_value)."""
    for q in ("Q1", "Q2", "Q3", "Q4"):
        wb = render_dc9_quarter(_clean_engagement(), q)
        ws = wb.active
        opening = ws.cell(row=19, column=2).value
        additions = ws.cell(row=20, column=2).value
        retirements = ws.cell(row=21, column=2).value
        closing = ws.cell(row=22, column=2).value
        asset_value = ws.cell(row=8, column=2).value

        assert closing == asset_value
        assert opening + additions - retirements == closing


# ── Attribute F (ownership share totals to 100%) ────────────────────


def test_ownership_percentages_sum_to_100() -> None:
    for q in ("Q1", "Q2", "Q3", "Q4"):
        wb = render_dc9_quarter(_clean_engagement(), q)
        ws = wb.active
        committed = [ws.cell(row=r, column=2).value for r in (26, 27, 28)]
        effective = [ws.cell(row=r, column=3).value for r in (26, 27, 28)]
        assert sum(committed) == 100
        assert sum(effective) == 100


def test_ownership_table_has_three_limited_partners() -> None:
    wb = render_dc9_quarter(_clean_engagement(), "Q1")
    ws = wb.active
    lp_names = [ws.cell(row=r, column=1).value for r in (26, 27, 28)]
    assert all(name and "LP" in name for name in lp_names)


# ── Determinism ─────────────────────────────────────────────────────


def test_render_dc9_quarter_is_deterministic_for_same_engagement() -> None:
    """Same spec + quarter → byte-identical workbook content."""
    eng = _clean_engagement()
    wb1 = render_dc9_quarter(eng, "Q2")
    wb2 = render_dc9_quarter(eng, "Q2")
    for r in range(1, 30):
        for c in range(1, 4):
            assert wb1.active.cell(row=r, column=c).value == wb2.active.cell(row=r, column=c).value


def test_different_quarters_differ_somewhere() -> None:
    eng = _clean_engagement()
    wb_q1 = render_dc9_quarter(eng, "Q1")
    wb_q3 = render_dc9_quarter(eng, "Q3")
    # At minimum the header row (quarter label) differs
    assert wb_q1.active.cell(row=2, column=2).value != wb_q3.active.cell(row=2, column=2).value
