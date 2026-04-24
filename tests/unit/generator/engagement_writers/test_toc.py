"""Tests for ``render_engagement_toc`` (Step 2, v2 corpus)."""

from __future__ import annotations

from agentic_audit.generator.engagement_writers.common import canonical_billing_fee
from agentic_audit.generator.engagement_writers.toc import render_engagement_toc
from agentic_audit.models.engagement import (
    EngagementSpec,
    QuarterControlSpec,
)


def _plan_engagement() -> EngagementSpec:
    """The §5 defect-placement plan — used as the canonical test fixture."""
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


def _clean_engagement() -> EngagementSpec:
    quarters = tuple(
        QuarterControlSpec(control_id=c, quarter=q, defect="none")  # type: ignore[arg-type]
        for c in ("DC-2", "DC-9")
        for q in ("Q1", "Q2", "Q3", "Q4")
    )
    return EngagementSpec(
        entity_name="Alpha Pension Fund",
        year=2025,
        seed=2025,
        quarters=quarters,
    )


# ── Structure ────────────────────────────────────────────────────────


def test_toc_has_exactly_dc2_and_dc9_sheets() -> None:
    wb = render_engagement_toc(_clean_engagement())
    assert wb.sheetnames == ["DC-2", "DC-9"]


def test_dc2_sheet_header_has_entity_and_control_label() -> None:
    wb = render_engagement_toc(_clean_engagement())
    ws = wb["DC-2"]
    assert ws.cell(row=1, column=2).value == "Alpha Pension Fund"
    assert ws.cell(row=1, column=5).value == "DC-2"
    assert ws.cell(row=2, column=2).value == "2025 (Q1–Q4)"


def test_dc9_sheet_header_has_entity_and_control_label() -> None:
    wb = render_engagement_toc(_clean_engagement())
    ws = wb["DC-9"]
    assert ws.cell(row=1, column=5).value == "DC-9"


def test_quarter_columns_are_q1_through_q4() -> None:
    wb = render_engagement_toc(_clean_engagement())
    for sheet in ("DC-2", "DC-9"):
        ws = wb[sheet]
        assert [ws.cell(row=4, column=c).value for c in (3, 4, 5, 6)] == ["Q1", "Q2", "Q3", "Q4"]


# ── Tickmark matrix — clean engagement ───────────────────────────────


def test_clean_engagement_all_tickmarks_are_a() -> None:
    wb = render_engagement_toc(_clean_engagement())
    for sheet, attr_count in (("DC-2", 4), ("DC-9", 6)):
        ws = wb[sheet]
        for r in range(5, 5 + attr_count):
            for c in (3, 4, 5, 6):
                assert ws.cell(row=r, column=c).value == "a", (
                    f"{sheet} row {r} col {c} should be 'a' for clean engagement"
                )


# ── Tickmark matrix — §5 defect placement ────────────────────────────


def test_plan_engagement_dc2_tickmarks_match_defect_placement() -> None:
    wb = render_engagement_toc(_plan_engagement())
    ws = wb["DC-2"]
    # Row 5 = A, 6 = B, 7 = C, 8 = D. Col 3 = Q1, 4 = Q2, 5 = Q3, 6 = Q4
    # Expected DC-2 matrix per §5:
    # A: a a a a
    # B: a a a X     (Q4 variance_no_explanation → B fails)
    # C: a a X a     (Q3 variance_explanation_inadequate → C fails)
    # D: a a a a
    assert [ws.cell(row=5, column=c).value for c in (3, 4, 5, 6)] == ["a", "a", "a", "a"]
    assert [ws.cell(row=6, column=c).value for c in (3, 4, 5, 6)] == ["a", "a", "a", "X"]
    assert [ws.cell(row=7, column=c).value for c in (3, 4, 5, 6)] == ["a", "a", "X", "a"]
    assert [ws.cell(row=8, column=c).value for c in (3, 4, 5, 6)] == ["a", "a", "a", "a"]


def test_plan_engagement_dc9_tickmarks_match_defect_placement() -> None:
    wb = render_engagement_toc(_plan_engagement())
    ws = wb["DC-9"]
    # Row 5 = A, 6 = B, 7 = C, 8 = D, 9 = E, 10 = F
    # A: a a a a
    # B: a a a a
    # C: a a X a     (Q3 figure_mismatch → C fails)
    # D: a a a X     (Q4 rate_change_without_amendment → D fails)
    # E: a a a a
    # F: a a a a
    assert [ws.cell(row=5, column=c).value for c in (3, 4, 5, 6)] == ["a", "a", "a", "a"]
    assert [ws.cell(row=6, column=c).value for c in (3, 4, 5, 6)] == ["a", "a", "a", "a"]
    assert [ws.cell(row=7, column=c).value for c in (3, 4, 5, 6)] == ["a", "a", "X", "a"]
    assert [ws.cell(row=8, column=c).value for c in (3, 4, 5, 6)] == ["a", "a", "a", "X"]
    assert [ws.cell(row=9, column=c).value for c in (3, 4, 5, 6)] == ["a", "a", "a", "a"]
    assert [ws.cell(row=10, column=c).value for c in (3, 4, 5, 6)] == ["a", "a", "a", "a"]


# ── Conclusion rows ──────────────────────────────────────────────────


def test_clean_engagement_overall_conclusion_is_effective() -> None:
    wb = render_engagement_toc(_clean_engagement())
    for sheet, conclusion_row in (("DC-2", 11), ("DC-9", 13)):
        ws = wb[sheet]
        assert ws.cell(row=conclusion_row, column=2).value == "Effective"
        assert ws.cell(row=conclusion_row + 1, column=2).value == "None"


def test_plan_engagement_overall_conclusion_lists_fail_quarters() -> None:
    wb = render_engagement_toc(_plan_engagement())

    ws_dc2 = wb["DC-2"]
    assert ws_dc2.cell(row=11, column=2).value == "Not effective"
    # Q3 (C fails), Q4 (B fails) — both quarters have at least one X
    assert ws_dc2.cell(row=12, column=2).value == "Q3, Q4"

    ws_dc9 = wb["DC-9"]
    assert ws_dc9.cell(row=13, column=2).value == "Not effective"
    # Q3 (C fails), Q4 (D fails)
    assert ws_dc9.cell(row=14, column=2).value == "Q3, Q4"


# ── Cross-file billing claim row (DC-9 only) ─────────────────────────


def test_dc9_billing_claim_row_present() -> None:
    wb = render_engagement_toc(_clean_engagement())
    ws = wb["DC-9"]
    assert ws.cell(row=12, column=1).value == "Billing fee per W/P (USD)"


def test_dc9_billing_claim_matches_canonical_for_non_figure_mismatch() -> None:
    """In the clean engagement every claim equals canonical_billing_fee."""
    eng = _clean_engagement()
    wb = render_engagement_toc(eng)
    ws = wb["DC-9"]
    for j, quarter in enumerate(("Q1", "Q2", "Q3", "Q4"), start=3):
        assert ws.cell(row=12, column=j).value == canonical_billing_fee(eng, quarter)  # type: ignore[arg-type]


def test_dc9_billing_claim_diverges_in_figure_mismatch_quarter() -> None:
    """Q3 under the §5 plan has figure_mismatch — claim must differ from
    canonical, offset within 1k–10% of the fee.
    """
    eng = _plan_engagement()
    wb = render_engagement_toc(eng)
    ws = wb["DC-9"]

    # Q1 / Q2 / Q4 should tie
    for col, quarter in ((3, "Q1"), (4, "Q2"), (6, "Q4")):
        canonical = canonical_billing_fee(eng, quarter)  # type: ignore[arg-type]
        assert ws.cell(row=12, column=col).value == canonical

    # Q3 diverges
    q3_claim = ws.cell(row=12, column=5).value
    q3_canonical = canonical_billing_fee(eng, "Q3")
    assert q3_claim != q3_canonical
    diff = abs(q3_claim - q3_canonical)
    assert 1_000 <= diff <= q3_canonical // 10


# ── Determinism ─────────────────────────────────────────────────────


def test_render_engagement_toc_is_deterministic() -> None:
    eng = _plan_engagement()
    wb1 = render_engagement_toc(eng)
    wb2 = render_engagement_toc(eng)
    for sheet in ("DC-2", "DC-9"):
        for r in range(1, 20):
            for c in range(1, 7):
                assert (
                    wb1[sheet].cell(row=r, column=c).value == wb2[sheet].cell(row=r, column=c).value
                )
