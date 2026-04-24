"""Tests for ``render_billing_calc`` (Task 12).

Writer-level contract — structure, determinism, content derived from
the seeded scenario. No cross-file consistency rules yet (those arrive
in Task 13).
"""

from __future__ import annotations

from agentic_audit.generator.workpaper_writers import render_billing_calc
from agentic_audit.models import ScenarioSpec, WorkpaperSpec


def _dc9_spec(seed: int = 42) -> ScenarioSpec:
    return ScenarioSpec(
        scenario_id="q1_pass_dc9_01",
        control_id="DC-9",
        pattern_type="signoff_with_tieout",
        quarter="Q1",
        expected_outcome="pass",
        exception_type="none",
        seed=seed,
    )


def _billing_calc_wp() -> WorkpaperSpec:
    return WorkpaperSpec(
        type="billing_calculation",
        filename="billing_calc.xlsx",
        toc_reference_code="DC-5.7",
    )


# ── structure ────────────────────────────────────────────────────────


def test_render_billing_calc_has_one_sheet_named_billing_calc() -> None:
    wb = render_billing_calc(_dc9_spec(), _billing_calc_wp())
    assert wb.sheetnames == ["Billing Calc"]


def test_render_billing_calc_has_expected_labels_in_column_a() -> None:
    wb = render_billing_calc(_dc9_spec(), _billing_calc_wp())
    ws = wb.active
    expected_labels = [
        "Billing Calculation — DC-5.7",
        "Entity",
        "Period",
        "Asset value (USD)",
        "Billing rate",
        "Billing fee (USD)",
        "Prepared by",
        "Date prepared",
    ]
    actual_labels = [ws.cell(row=r, column=1).value for r in range(1, 9)]
    assert actual_labels == expected_labels


def test_render_billing_calc_title_row_reflects_toc_reference_code() -> None:
    wp = WorkpaperSpec(
        type="billing_calculation",
        filename="billing_calc.xlsx",
        toc_reference_code="DC-9.9",
    )
    wb = render_billing_calc(_dc9_spec(), wp)
    ws = wb.active
    assert ws.cell(row=1, column=1).value == "Billing Calculation — DC-9.9"


# ── content types + computation ──────────────────────────────────────


def test_render_billing_calc_asset_value_is_positive_int() -> None:
    wb = render_billing_calc(_dc9_spec(), _billing_calc_wp())
    ws = wb.active
    asset_value = ws.cell(row=4, column=2).value
    assert isinstance(asset_value, int)
    assert 10_000_000 <= asset_value <= 500_000_000


def test_render_billing_calc_fee_equals_assets_times_rate() -> None:
    wb = render_billing_calc(_dc9_spec(), _billing_calc_wp())
    ws = wb.active
    asset_value = ws.cell(row=4, column=2).value
    rate_str = ws.cell(row=5, column=2).value
    billing_fee = ws.cell(row=6, column=2).value

    rate_decimal = float(rate_str.rstrip("%")) / 100.0
    expected_fee = int(asset_value * rate_decimal)
    assert billing_fee == expected_fee


def test_render_billing_calc_rate_comes_from_discrete_pool() -> None:
    wb = render_billing_calc(_dc9_spec(), _billing_calc_wp())
    ws = wb.active
    rate_str = ws.cell(row=5, column=2).value
    assert rate_str in ("0.25%", "0.50%", "0.75%", "1.00%")


def test_render_billing_calc_period_format_is_quarter_year() -> None:
    wb = render_billing_calc(_dc9_spec(), _billing_calc_wp())
    ws = wb.active
    period = ws.cell(row=3, column=2).value
    assert period.startswith("Q1 ")
    assert period.endswith("2025")


def test_render_billing_calc_entity_from_greek_pool() -> None:
    wb = render_billing_calc(_dc9_spec(), _billing_calc_wp())
    ws = wb.active
    entity = ws.cell(row=2, column=2).value
    greek = (
        "Alpha",
        "Beta",
        "Gamma",
        "Delta",
        "Epsilon",
        "Zeta",
        "Eta",
        "Theta",
    )
    assert any(entity.startswith(letter + " ") for letter in greek)


# ── determinism ──────────────────────────────────────────────────────


def test_render_billing_calc_same_seed_same_values() -> None:
    wb1 = render_billing_calc(_dc9_spec(seed=42), _billing_calc_wp())
    wb2 = render_billing_calc(_dc9_spec(seed=42), _billing_calc_wp())
    ws1, ws2 = wb1.active, wb2.active
    for r in range(1, 9):
        for c in range(1, 3):
            assert ws1.cell(row=r, column=c).value == ws2.cell(row=r, column=c).value


def test_render_billing_calc_different_seeds_differ_in_entity_or_values() -> None:
    """Two different seeds should produce different entity-or-value combinations."""
    wb1 = render_billing_calc(_dc9_spec(seed=42), _billing_calc_wp())
    wb2 = render_billing_calc(_dc9_spec(seed=99), _billing_calc_wp())
    ws1, ws2 = wb1.active, wb2.active

    diffs = 0
    for r in range(2, 9):
        for c in range(1, 3):
            if ws1.cell(row=r, column=c).value != ws2.cell(row=r, column=c).value:
                diffs += 1
    assert diffs > 0
