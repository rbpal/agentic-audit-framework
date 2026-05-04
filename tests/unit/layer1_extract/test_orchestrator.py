"""Unit tests for `agentic_audit.layer1_extract.orchestrator.extract`.

Tests orchestrator wiring (read bronze → run N checks → assemble record),
not check logic (that's tested in `test_attribute_checks.py` against real
xlsx fixtures).

`check_attribute` is monkeypatched to a deterministic stub for every test
so the orchestrator behavior is observable without depending on real
workpaper structure. Sign-off parsing is exercised against synthetic
rows that include the expected r4/r5 (DC-9) or r17 (DC-2) cells.

What's covered:

- Happy path DC-9 Q1: 6 AttributeChecks (A-F).
- Happy path DC-2 Q3: 4 AttributeChecks (A-D).
- Empty bronze rows raises `ExtractionError` (with helpful hint).
- Determinism: 100 calls with pinned `run_id` + timestamp produce
  structurally identical records.
- `run_id` and `extraction_timestamp` defaults are generated when not
  supplied — and produce non-empty / tz-aware values.
- `source_bronze_file_hash` is taken from the first row.
- Sign-off parsing for DC-9 (r4 preparer + r5 reviewer) and DC-2
  (r17 reviewer + synthetic preparer).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from agentic_audit.layer1_extract.bronze_reader import (
    BronzeWorkpaperRow,
    ExtractionError,
)
from agentic_audit.layer1_extract.orchestrator import extract
from agentic_audit.models.evidence import (
    ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
    ExtractedEvidence,
)

UTC_TS = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
PINNED_RUN_ID = "01J0F7M5XQXM2QYAY8X8X8X8X8"


@pytest.fixture(autouse=True)
def stub_check_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `check_attribute` with a deterministic pass-stub for orchestrator
    tests. Real check logic is exercised by `test_attribute_checks.py`."""

    def stub(control_id, attribute_id, rows, toc):  # type: ignore[no-untyped-def]
        return AttributeCheck(
            control_id=control_id,
            attribute_id=attribute_id,
            status="pass",
            evidence_cell_refs=[],
        )

    monkeypatch.setattr("agentic_audit.layer1_extract.orchestrator.check_attribute", stub)


def _row(
    *,
    control_id: str = "DC-9",
    quarter: str = "Q1",
    sheet_name: str | None = None,
    row_index: int = 1,
    file_hash: str = "a" * 64,
    raw_data: dict[str, str] | None = None,
) -> BronzeWorkpaperRow:
    if sheet_name is None:
        sheet_name = "DC-9 Billing" if control_id == "DC-9" else "DC-2 Variance"
    return BronzeWorkpaperRow(
        source_path=f"/Volumes/audit_dev/bronze/raw_pdfs/corpus/v2/workpapers/dc{control_id.split('-')[1]}_{quarter}_ref.xlsx",
        file_hash=file_hash,
        engagement_id="alpha-pension-fund-2025",
        control_id=control_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        sheet_name=sheet_name,
        row_index=row_index,
        raw_data=raw_data if raw_data is not None else {"col_a": "val_a"},
        ingested_at=UTC_TS,
        ingested_by="smoke-ingest",
    )


def _dc9_rows_with_signoffs(
    *,
    quarter: str = "Q1",
    n_filler: int = 3,
) -> list[BronzeWorkpaperRow]:
    """Build a minimal DC-9 row set: r4 preparer, r5 reviewer, plus filler."""
    rows = [
        _row(
            control_id="DC-9",
            quarter=quarter,
            row_index=4,
            raw_data={"col_00": "Preparer (Attribute A)", "col_01": "AB — 2026-05-01"},
        ),
        _row(
            control_id="DC-9",
            quarter=quarter,
            row_index=5,
            raw_data={"col_00": "Reviewer (Attribute B)", "col_01": "CD — 2026-05-02"},
        ),
    ]
    rows.extend(_row(control_id="DC-9", quarter=quarter, row_index=10 + i) for i in range(n_filler))
    return rows


def _dc2_rows_with_signoff(
    *,
    quarter: str = "Q3",
    n_filler: int = 3,
) -> list[BronzeWorkpaperRow]:
    """Build a minimal DC-2 row set: r17 reviewer, plus filler. No preparer
    cell — orchestrator synthesises one with initials='AU'."""
    rows = [
        _row(
            control_id="DC-2",
            quarter=quarter,
            row_index=17,
            raw_data={"col_00": "Reviewer (Attribute D)", "col_01": "PZ — 2026-05-03"},
        ),
    ]
    rows.extend(_row(control_id="DC-2", quarter=quarter, row_index=10 + i) for i in range(n_filler))
    return rows


def _reader_returning(rows: list[BronzeWorkpaperRow]) -> MagicMock:
    reader = MagicMock()
    reader.read.return_value = rows
    return reader


# ---------- happy paths --------------------------------------------------


def test_extract_dc9_q1_returns_six_attribute_checks() -> None:
    rows = _dc9_rows_with_signoffs(quarter="Q1")
    reader = _reader_returning(rows)

    result = extract(
        "alpha-pension-fund-2025",
        "DC-9",
        "Q1",
        bronze_reader=reader,
        run_id=PINNED_RUN_ID,
        extraction_timestamp=UTC_TS,
    )

    assert isinstance(result, ExtractedEvidence)
    assert result.control_id == "DC-9"
    assert result.quarter == "Q1"
    assert [a.attribute_id for a in result.attributes] == ["A", "B", "C", "D", "E", "F"]
    assert all(a.status == "pass" for a in result.attributes)  # stubbed by fixture
    assert result.run_id == PINNED_RUN_ID
    assert result.extraction_timestamp == UTC_TS
    assert result.source_bronze_file_hash == "a" * 64
    reader.read.assert_called_once_with("alpha-pension-fund-2025", "DC-9", "Q1")


def test_extract_dc2_q3_returns_four_attribute_checks() -> None:
    rows = _dc2_rows_with_signoff(quarter="Q3")
    reader = _reader_returning(rows)

    result = extract(
        "alpha-pension-fund-2025",
        "DC-2",
        "Q3",
        bronze_reader=reader,
        run_id=PINNED_RUN_ID,
        extraction_timestamp=UTC_TS,
    )

    assert result.control_id == "DC-2"
    assert [a.attribute_id for a in result.attributes] == ["A", "B", "C", "D"]
    assert len(result.attributes) == 4


# ---------- error paths --------------------------------------------------


def test_extract_empty_rows_raises_extraction_error() -> None:
    reader = _reader_returning([])
    with pytest.raises(ExtractionError, match="no bronze rows"):
        extract(
            "alpha-pension-fund-2025",
            "DC-9",
            "Q1",
            bronze_reader=reader,
        )


def test_extract_reader_failure_propagates() -> None:
    reader = MagicMock()
    reader.read.side_effect = ConnectionError("warehouse down")
    with pytest.raises(ConnectionError, match="warehouse down"):
        extract(
            "alpha-pension-fund-2025",
            "DC-9",
            "Q1",
            bronze_reader=reader,
        )


# ---------- determinism --------------------------------------------------


def test_extract_is_deterministic_with_pinned_inputs() -> None:
    """Same bronze rows + pinned run_id + pinned timestamp → 100×
    structurally identical records."""
    rows = _dc9_rows_with_signoffs(quarter="Q1")

    results = [
        extract(
            "alpha-pension-fund-2025",
            "DC-9",
            "Q1",
            bronze_reader=_reader_returning(rows),
            run_id=PINNED_RUN_ID,
            extraction_timestamp=UTC_TS,
        )
        for _ in range(100)
    ]

    first = results[0].model_dump()
    assert all(r.model_dump() == first for r in results)


# ---------- defaults -----------------------------------------------------


def test_extract_generates_run_id_when_not_supplied() -> None:
    rows = _dc9_rows_with_signoffs()
    result_a = extract(
        "alpha-pension-fund-2025",
        "DC-9",
        "Q1",
        bronze_reader=_reader_returning(rows),
        extraction_timestamp=UTC_TS,
    )
    result_b = extract(
        "alpha-pension-fund-2025",
        "DC-9",
        "Q1",
        bronze_reader=_reader_returning(rows),
        extraction_timestamp=UTC_TS,
    )
    assert result_a.run_id != result_b.run_id
    assert len(result_a.run_id) > 0
    assert len(result_b.run_id) > 0


def test_extract_generates_timestamp_when_not_supplied() -> None:
    before = datetime.now(UTC)
    rows = _dc9_rows_with_signoffs()
    result = extract(
        "alpha-pension-fund-2025",
        "DC-9",
        "Q1",
        bronze_reader=_reader_returning(rows),
        run_id=PINNED_RUN_ID,
    )
    after = datetime.now(UTC)
    assert before <= result.extraction_timestamp <= after
    assert result.extraction_timestamp.tzinfo is not None  # tz-aware


# ---------- coverage map sanity ------------------------------------------


def test_attribute_count_matches_per_control_map() -> None:
    """Belt-and-suspenders: orchestrator output length matches the
    documented per-control coverage."""
    for control, expected_attrs in ATTRIBUTES_PER_CONTROL.items():
        rows = (
            _dc9_rows_with_signoffs(quarter="Q1")
            if control == "DC-9"
            else _dc2_rows_with_signoff(quarter="Q1")
        )
        result = extract(
            "alpha-pension-fund-2025",
            control,  # type: ignore[arg-type]
            "Q1",
            bronze_reader=_reader_returning(rows),
            run_id=PINNED_RUN_ID,
            extraction_timestamp=UTC_TS,
        )
        assert [a.attribute_id for a in result.attributes] == expected_attrs


# ---------- signoff parsing ----------------------------------------------


def test_dc9_signoffs_parsed_from_rows_4_and_5() -> None:
    rows = _dc9_rows_with_signoffs(quarter="Q1")
    result = extract(
        "alpha-pension-fund-2025",
        "DC-9",
        "Q1",
        bronze_reader=_reader_returning(rows),
        run_id=PINNED_RUN_ID,
        extraction_timestamp=UTC_TS,
    )
    assert result.preparer.initials == "AB"
    assert result.preparer.role == "preparer"
    assert result.preparer.date.date().isoformat() == "2026-05-01"
    assert result.reviewer.initials == "CD"
    assert result.reviewer.role == "reviewer"
    assert result.reviewer.date.date().isoformat() == "2026-05-02"


def test_dc2_reviewer_parsed_preparer_synthesised() -> None:
    rows = _dc2_rows_with_signoff(quarter="Q3")
    result = extract(
        "alpha-pension-fund-2025",
        "DC-2",
        "Q3",
        bronze_reader=_reader_returning(rows),
        run_id=PINNED_RUN_ID,
        extraction_timestamp=UTC_TS,
    )
    assert result.reviewer.initials == "PZ"
    assert result.reviewer.date.date().isoformat() == "2026-05-03"
    # No preparer cell in DC-2 → synthetic 'AU' with date == ingested_at.
    assert result.preparer.initials == "AU"
    assert result.preparer.role == "preparer"
    assert result.preparer.date == UTC_TS


def test_dc9_signoffs_fallback_when_cells_missing() -> None:
    """If r4/r5 are absent, signoffs fall back to '??' + ingested_at date."""
    rows = [_row(control_id="DC-9", quarter="Q1", row_index=10)]  # only r10, no r4/r5
    result = extract(
        "alpha-pension-fund-2025",
        "DC-9",
        "Q1",
        bronze_reader=_reader_returning(rows),
        run_id=PINNED_RUN_ID,
        extraction_timestamp=UTC_TS,
    )
    assert result.preparer.initials == "??"
    assert result.reviewer.initials == "??"
    assert result.preparer.date == UTC_TS
    assert result.reviewer.date == UTC_TS
