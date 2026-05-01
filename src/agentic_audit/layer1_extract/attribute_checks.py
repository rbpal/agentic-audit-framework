"""Per-control attribute checks (single-file workpaper checks).

Implements the 10 dispatch branches:

- DC-2 × {A, B, C, D} — variance analysis (data-load tie-out, variance
  explanation, source tie, reviewer sign-off).
- DC-9 × {A, B, C, D, E, F} — billing sign-off + tie-out (preparer
  sign-off, reviewer sign-off, billing-fee math tie, rate-change with
  amendment, asset roll-forward, ownership share).

All checks operate on the `BronzeWorkpaperRow` list returned by
`BronzeReader.read(...)` for one `(engagement, control, quarter)`
triple. **Single-file only** — cross-file checks (TOC claim vs W/P
figure) live in `silver.cross_file_validations`, not here. See
`task_03.1` of `step_04_layer1_extraction.md` for the full mapping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentic_audit.models.engagement import ControlId
from agentic_audit.models.evidence import (
    ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
    AttributeId,
)
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentic_audit.layer1_extract.bronze_reader import BronzeWorkpaperRow

    _CheckImpl = Callable[[list["BronzeWorkpaperRow"], Any], AttributeCheck]


_DC2_SHEET = "DC-2 Variance"
_DC9_SHEET = "DC-9 Billing"

# Variance table on DC-2 spans rows 10..14 inclusive (5 revenue streams).
_DC2_VAR_ROW_RANGE = range(10, 15)


# ---- Helpers ------------------------------------------------------------


def _find_row(
    rows: list[BronzeWorkpaperRow],
    sheet_name: str,
    row_index: int,
) -> BronzeWorkpaperRow | None:
    for r in rows:
        if r.sheet_name == sheet_name and r.row_index == row_index:
            return r
    return None


def _cell(row: BronzeWorkpaperRow | None, col_key: str) -> str | None:
    if row is None:
        return None
    return row.raw_data.get(col_key)


def _parse_currency(value: str | None) -> float | None:
    """Parse '18665242', '18,665,242', '$18,665,242.00' → 18665242.0.
    Returns None if value is None or unparseable."""
    if value is None:
        return None
    cleaned = value.replace(",", "").replace("$", "").replace(" ", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_percent(value: str | None) -> float | None:
    """Parse '0.25%' → 0.0025. Returns None if value is None, contains
    'N/A', or doesn't end in '%'."""
    if value is None:
        return None
    s = value.strip()
    if "N/A" in s or "n/a" in s:
        return None
    if not s.endswith("%"):
        return None
    try:
        return float(s[:-1].strip()) / 100.0
    except ValueError:
        return None


def _ref(sheet: str, row_index: int, col_key: str) -> str:
    """Build an evidence_cell_ref string. col_key is '00'..'NN' (zero-padded
    column index); we surface it as a 0-based column number for traceability."""
    col_num = int(col_key.removeprefix("col_"))
    return f"{sheet}!r{row_index}c{col_num}"


# ---- DC-2 implementations -----------------------------------------------


def _dc2_a(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    """Data-load tie-out: upstream feed total == workbook total + tie flag."""
    r6 = _find_row(rows, _DC2_SHEET, 6)
    r7 = _find_row(rows, _DC2_SHEET, 7)
    feed = _parse_currency(_cell(r6, "col_01"))
    book = _parse_currency(_cell(r7, "col_01"))
    tie_flag = _cell(r7, "col_02") or ""

    refs = [
        _ref(_DC2_SHEET, 6, "col_01"),
        _ref(_DC2_SHEET, 7, "col_01"),
        _ref(_DC2_SHEET, 7, "col_02"),
    ]
    if feed is None or book is None:
        return AttributeCheck(
            control_id="DC-2",
            attribute_id="A",
            status="fail",
            evidence_cell_refs=refs,
            notes="missing upstream feed total or workbook total",
        )
    if abs(feed - book) > 1.0:
        return AttributeCheck(
            control_id="DC-2",
            attribute_id="A",
            status="fail",
            evidence_cell_refs=refs,
            extracted_value={"feed_total": feed, "workbook_total": book},
            notes=f"workbook total {book} does not tie to upstream feed {feed}",
        )
    if "Yes" not in tie_flag:
        return AttributeCheck(
            control_id="DC-2",
            attribute_id="A",
            status="fail",
            evidence_cell_refs=refs,
            notes=f"tie flag does not affirm match: {tie_flag!r}",
        )
    return AttributeCheck(
        control_id="DC-2",
        attribute_id="A",
        status="pass",
        evidence_cell_refs=refs,
        extracted_value={"feed_total": feed, "workbook_total": book},
    )


def _dc2_above_threshold_rows(
    rows: list[BronzeWorkpaperRow],
) -> list[BronzeWorkpaperRow]:
    """Return rows 10–14 of the DC-2 sheet where col_05 == 'Yes'."""
    out = []
    for i in _DC2_VAR_ROW_RANGE:
        r = _find_row(rows, _DC2_SHEET, i)
        if r is not None and (_cell(r, "col_05") or "").strip() == "Yes":
            out.append(r)
    return out


def _dc2_b(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    """Variance has explanation: every above-threshold row has non-empty col_06."""
    above = _dc2_above_threshold_rows(rows)
    refs = [_ref(_DC2_SHEET, r.row_index, "col_06") for r in above]
    failures = [r.row_index for r in above if not (_cell(r, "col_06") or "").strip()]
    if failures:
        return AttributeCheck(
            control_id="DC-2",
            attribute_id="B",
            status="fail",
            evidence_cell_refs=refs,
            extracted_value={"rows_missing_explanation": failures},
            notes=f"{len(failures)} above-threshold row(s) missing Explanation: rows {failures}",
        )
    return AttributeCheck(
        control_id="DC-2",
        attribute_id="B",
        status="pass",
        evidence_cell_refs=refs,
        extracted_value={"above_threshold_rows": [r.row_index for r in above]},
    )


def _dc2_c(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    """Variance has source tie: every above-threshold row that has an
    explanation has col_07 == 'Yes'.

    Rows with a blank/missing explanation are out of scope here — that's
    DC-2.B's failure mode. The TOC's gold answer treats no-explanation
    rows as B-only failures (e.g. Q4 dc2_variance_no_explanation), so
    C's check skips those rows rather than double-counting.
    """
    above = _dc2_above_threshold_rows(rows)
    rows_with_explanation = [r for r in above if (_cell(r, "col_06") or "").strip()]
    refs = [_ref(_DC2_SHEET, r.row_index, "col_07") for r in rows_with_explanation]
    failures = [
        r.row_index for r in rows_with_explanation if (_cell(r, "col_07") or "").strip() != "Yes"
    ]
    if failures:
        return AttributeCheck(
            control_id="DC-2",
            attribute_id="C",
            status="fail",
            evidence_cell_refs=refs,
            extracted_value={"rows_with_source_tie_no": failures},
            notes=f"{len(failures)} above-threshold row(s) with Source tie != 'Yes': rows {failures}",
        )
    return AttributeCheck(
        control_id="DC-2",
        attribute_id="C",
        status="pass",
        evidence_cell_refs=refs,
        extracted_value={"checked_rows": [r.row_index for r in rows_with_explanation]},
    )


def _dc2_d(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    """Reviewer sign-off: row 17 col_01 has '<initials> — <date>'."""
    r17 = _find_row(rows, _DC2_SHEET, 17)
    refs = [_ref(_DC2_SHEET, 17, "col_01")]
    val = _cell(r17, "col_01")
    if val is None or "—" not in val:
        return AttributeCheck(
            control_id="DC-2",
            attribute_id="D",
            status="fail",
            evidence_cell_refs=refs,
            notes=f"reviewer cell missing or unparseable: {val!r}",
        )
    return AttributeCheck(
        control_id="DC-2",
        attribute_id="D",
        status="pass",
        evidence_cell_refs=refs,
        extracted_value=val,
    )


# ---- DC-9 implementations -----------------------------------------------


def _dc9_signoff(
    rows: list[BronzeWorkpaperRow],
    *,
    row_index: int,
    attribute_id: AttributeId,
) -> AttributeCheck:
    """Shared logic for DC-9.A (preparer, row 4) and DC-9.B (reviewer, row 5)."""
    r = _find_row(rows, _DC9_SHEET, row_index)
    refs = [_ref(_DC9_SHEET, row_index, "col_01")]
    val = _cell(r, "col_01")
    if val is None or "—" not in val:
        return AttributeCheck(
            control_id="DC-9",
            attribute_id=attribute_id,
            status="fail",
            evidence_cell_refs=refs,
            notes=f"sign-off cell missing or unparseable: {val!r}",
        )
    return AttributeCheck(
        control_id="DC-9",
        attribute_id=attribute_id,
        status="pass",
        evidence_cell_refs=refs,
        extracted_value=val,
    )


def _dc9_a(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    return _dc9_signoff(rows, row_index=4, attribute_id="A")


def _dc9_b(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    return _dc9_signoff(rows, row_index=5, attribute_id="B")


def _dc9_c(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    """Billing-fee math tie: asset_value × billing_rate ≈ billing_fee.
    Single-file only — cross-file TOC discrepancies are
    silver.cross_file_validations."""
    r8 = _find_row(rows, _DC9_SHEET, 8)
    r9 = _find_row(rows, _DC9_SHEET, 9)
    r10 = _find_row(rows, _DC9_SHEET, 10)
    refs = [
        _ref(_DC9_SHEET, 8, "col_01"),
        _ref(_DC9_SHEET, 9, "col_01"),
        _ref(_DC9_SHEET, 10, "col_01"),
    ]
    asset = _parse_currency(_cell(r8, "col_01"))
    rate = _parse_percent(_cell(r9, "col_01"))
    fee = _parse_currency(_cell(r10, "col_01"))

    if asset is None or rate is None or fee is None:
        return AttributeCheck(
            control_id="DC-9",
            attribute_id="C",
            status="fail",
            evidence_cell_refs=refs,
            notes=f"missing or unparseable input: asset={asset}, rate={rate}, fee={fee}",
        )
    expected = asset * rate
    if abs(fee - expected) > 1.0:
        return AttributeCheck(
            control_id="DC-9",
            attribute_id="C",
            status="fail",
            evidence_cell_refs=refs,
            extracted_value={"asset": asset, "rate": rate, "fee": fee, "expected_fee": expected},
            notes=f"billing fee {fee} does not tie to asset×rate={expected:.2f} (delta {fee - expected:.2f})",
        )
    return AttributeCheck(
        control_id="DC-9",
        attribute_id="C",
        status="pass",
        evidence_cell_refs=refs,
        extracted_value={"asset": asset, "rate": rate, "fee": fee},
    )


def _dc9_d(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    """Rate change with amendment.

    n/a if Q1 (no prior period — r13 starts with 'N/A').
    pass if rates equal (no change to amend).
    pass if rates differ AND amendment text is a real reference.
    fail if rates differ AND amendment is missing or 'NO AMENDMENT FILED'.
    """
    r13 = _find_row(rows, _DC9_SHEET, 13)  # Prior rate
    r14 = _find_row(rows, _DC9_SHEET, 14)  # Current rate
    r16 = _find_row(rows, _DC9_SHEET, 16)  # Supporting amendment
    refs = [
        _ref(_DC9_SHEET, 13, "col_01"),
        _ref(_DC9_SHEET, 14, "col_01"),
        _ref(_DC9_SHEET, 16, "col_01"),
    ]
    prior_raw = _cell(r13, "col_01") or ""
    if "N/A" in prior_raw or "no prior" in prior_raw.lower():
        return AttributeCheck(
            control_id="DC-9",
            attribute_id="D",
            status="n/a",
            evidence_cell_refs=refs,
            notes="Q1 has no prior period; comparison not applicable",
        )
    prior = _parse_percent(prior_raw)
    current = _parse_percent(_cell(r14, "col_01"))
    amendment = (_cell(r16, "col_01") or "").strip()

    if prior is None or current is None:
        return AttributeCheck(
            control_id="DC-9",
            attribute_id="D",
            status="fail",
            evidence_cell_refs=refs,
            notes=f"unparseable rates: prior={prior_raw!r}, current={_cell(r14, 'col_01')!r}",
        )
    if abs(prior - current) < 1e-9:
        # No rate change → amendment not required.
        return AttributeCheck(
            control_id="DC-9",
            attribute_id="D",
            status="pass",
            evidence_cell_refs=refs,
            extracted_value={"prior_rate": prior, "current_rate": current, "rate_change": False},
        )
    # Rates differ → amendment must be a real reference.
    if not amendment or "NO AMENDMENT FILED" in amendment.upper() or amendment.startswith("N/A"):
        return AttributeCheck(
            control_id="DC-9",
            attribute_id="D",
            status="fail",
            evidence_cell_refs=refs,
            extracted_value={"prior_rate": prior, "current_rate": current, "amendment": amendment},
            notes=f"rates differ (prior={prior}, current={current}) but amendment is {amendment!r}",
        )
    return AttributeCheck(
        control_id="DC-9",
        attribute_id="D",
        status="pass",
        evidence_cell_refs=refs,
        extracted_value={"prior_rate": prior, "current_rate": current, "amendment": amendment},
    )


def _dc9_e(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    """Asset roll-forward: closing == opening + additions − retirements.

    The workpaper stores retirements as a delta (typically negative when
    additions outpaced asset growth, positive otherwise). The corpus
    generator computes ``retirements = opening + additions - asset_value``,
    so the tie equation is ``closing = opening + additions - retirements``.
    """
    r19 = _find_row(rows, _DC9_SHEET, 19)
    r20 = _find_row(rows, _DC9_SHEET, 20)
    r21 = _find_row(rows, _DC9_SHEET, 21)
    r22 = _find_row(rows, _DC9_SHEET, 22)
    refs = [
        _ref(_DC9_SHEET, 19, "col_01"),
        _ref(_DC9_SHEET, 20, "col_01"),
        _ref(_DC9_SHEET, 21, "col_01"),
        _ref(_DC9_SHEET, 22, "col_01"),
    ]
    opening = _parse_currency(_cell(r19, "col_01"))
    additions = _parse_currency(_cell(r20, "col_01"))
    retirements = _parse_currency(_cell(r21, "col_01"))
    closing = _parse_currency(_cell(r22, "col_01"))

    if None in (opening, additions, retirements, closing):
        return AttributeCheck(
            control_id="DC-9",
            attribute_id="E",
            status="fail",
            evidence_cell_refs=refs,
            notes=f"missing input: opening={opening}, additions={additions}, retirements={retirements}, closing={closing}",
        )
    # Type narrowing for mypy after the None check above
    assert opening is not None and additions is not None  # noqa: S101
    assert retirements is not None and closing is not None  # noqa: S101
    expected = opening + additions - retirements
    if abs(closing - expected) > 1.0:
        return AttributeCheck(
            control_id="DC-9",
            attribute_id="E",
            status="fail",
            evidence_cell_refs=refs,
            extracted_value={
                "opening": opening,
                "additions": additions,
                "retirements": retirements,
                "closing": closing,
                "expected_closing": expected,
            },
            notes=f"closing {closing} does not tie to opening+additions-retirements={expected:.2f}",
        )
    return AttributeCheck(
        control_id="DC-9",
        attribute_id="E",
        status="pass",
        evidence_cell_refs=refs,
        extracted_value={
            "opening": opening,
            "additions": additions,
            "retirements": retirements,
            "closing": closing,
        },
    )


def _dc9_f(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    """Ownership share: sum of effective % across LP rows == 100."""
    lp_rows = [
        r
        for r in rows
        if r.sheet_name == _DC9_SHEET and r.row_index >= 26 and (_cell(r, "col_00") or "").strip()
    ]
    refs = [_ref(_DC9_SHEET, r.row_index, "col_02") for r in lp_rows]
    pcts: list[float] = []
    for r in lp_rows:
        v = _parse_currency(_cell(r, "col_02"))
        if v is None:
            return AttributeCheck(
                control_id="DC-9",
                attribute_id="F",
                status="fail",
                evidence_cell_refs=refs,
                notes=f"unparseable Effective % on row {r.row_index}: {_cell(r, 'col_02')!r}",
            )
        pcts.append(v)
    total = sum(pcts)
    if abs(total - 100.0) > 0.01:
        return AttributeCheck(
            control_id="DC-9",
            attribute_id="F",
            status="fail",
            evidence_cell_refs=refs,
            extracted_value={"effective_pcts": pcts, "total": total},
            notes=f"sum of Effective % = {total} (expected 100)",
        )
    return AttributeCheck(
        control_id="DC-9",
        attribute_id="F",
        status="pass",
        evidence_cell_refs=refs,
        extracted_value={"effective_pcts": pcts, "total": total},
    )


# ---- Dispatch -----------------------------------------------------------


_DISPATCH: dict[tuple[ControlId, AttributeId], _CheckImpl] = {
    ("DC-2", "A"): _dc2_a,
    ("DC-2", "B"): _dc2_b,
    ("DC-2", "C"): _dc2_c,
    ("DC-2", "D"): _dc2_d,
    ("DC-9", "A"): _dc9_a,
    ("DC-9", "B"): _dc9_b,
    ("DC-9", "C"): _dc9_c,
    ("DC-9", "D"): _dc9_d,
    ("DC-9", "E"): _dc9_e,
    ("DC-9", "F"): _dc9_f,
}


@traced_function("layer1.check_attribute")
def check_attribute(
    control_id: ControlId,
    attribute_id: AttributeId,
    rows: list[BronzeWorkpaperRow],
    toc: Any,
) -> AttributeCheck:
    """Dispatch entry point.

    Looks up the implementation for ``(control_id, attribute_id)`` and
    calls it. Raises ``KeyError`` if the pair is not registered (e.g.
    DC-2 doesn't define attribute E).

    The ``toc`` parameter is accepted for forward-compat (Step 5 may add
    cross-file TOC checks at this layer); current implementations all
    ignore it. Single-file checks only — cross-file fails surface in
    `silver.cross_file_validations`.
    """
    if attribute_id not in ATTRIBUTES_PER_CONTROL[control_id]:
        raise KeyError(
            f"control_id={control_id} does not define attribute_id={attribute_id}; "
            f"valid attributes for {control_id}: {ATTRIBUTES_PER_CONTROL[control_id]}"
        )
    return _DISPATCH[(control_id, attribute_id)](rows, toc)


__all__ = ["check_attribute"]
