"""Unit tests for `agentic_audit.layer1_extract.orchestrator.extract`.

Mocks `BronzeReader` (interface tested in PR #47). Uses the real stub
`check_attribute` from `attribute_checks.py` — every attribute returns
``status="pass"`` until task_03 lands real impls.

What's covered:

- Happy path DC-9 Q1: 6 AttributeChecks (A-F), all `pass`.
- Happy path DC-2 Q1: 4 AttributeChecks (A-D), all `pass`.
- Empty bronze rows raises `ExtractionError` (with helpful hint).
- Determinism: 100 calls with pinned `run_id` + timestamp produce
  structurally identical records.
- `run_id` and `extraction_timestamp` defaults are generated when not
  supplied — and produce non-empty / tz-aware values.
- `source_bronze_file_hash` is taken from the first row.
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
    ExtractedEvidence,
)

UTC_TS = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
PINNED_RUN_ID = "01J0F7M5XQXM2QYAY8X8X8X8X8"


def _row(
    *,
    control_id: str = "DC-9",
    quarter: str = "Q1",
    sheet_name: str = "DC9_WP",
    row_index: int = 1,
    file_hash: str = "a" * 64,
) -> BronzeWorkpaperRow:
    return BronzeWorkpaperRow(
        ingestion_id=row_index,
        source_path=f"/Volumes/audit_dev/bronze/raw_pdfs/corpus/v2/workpapers/dc{control_id.split('-')[1]}_{quarter}_ref.xlsx",
        file_hash=file_hash,
        engagement_id="alpha-pension-fund-2025",
        control_id=control_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        sheet_name=sheet_name,
        row_index=row_index,
        raw_data={"col_a": "val_a"},
        ingested_at=UTC_TS,
        ingested_by="smoke-ingest",
    )


def _reader_returning(rows: list[BronzeWorkpaperRow]) -> MagicMock:
    reader = MagicMock()
    reader.read.return_value = rows
    return reader


# ---------- happy paths --------------------------------------------------


def test_extract_dc9_q1_returns_six_attribute_checks() -> None:
    rows = [_row(control_id="DC-9", quarter="Q1", row_index=i) for i in range(1, 11)]
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
    assert all(a.status == "pass" for a in result.attributes)
    assert result.run_id == PINNED_RUN_ID
    assert result.extraction_timestamp == UTC_TS
    assert result.source_bronze_file_hash == "a" * 64
    reader.read.assert_called_once_with("alpha-pension-fund-2025", "DC-9", "Q1")


def test_extract_dc2_q3_returns_four_attribute_checks() -> None:
    rows = [_row(control_id="DC-2", quarter="Q3", row_index=i) for i in range(1, 6)]
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
    rows = [_row(control_id="DC-9", quarter="Q1", row_index=i) for i in range(1, 4)]

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
    rows = [_row()]
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
    rows = [_row()]
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
        rows = [_row(control_id=control, quarter="Q1")]
        result = extract(
            "alpha-pension-fund-2025",
            control,  # type: ignore[arg-type]
            "Q1",
            bronze_reader=_reader_returning(rows),
            run_id=PINNED_RUN_ID,
            extraction_timestamp=UTC_TS,
        )
        assert [a.attribute_id for a in result.attributes] == expected_attrs


# ---------- signoff stub ------------------------------------------------


def test_signoffs_use_ingested_at_as_date() -> None:
    rows = [_row(control_id="DC-9", quarter="Q1")]
    result = extract(
        "alpha-pension-fund-2025",
        "DC-9",
        "Q1",
        bronze_reader=_reader_returning(rows),
        run_id=PINNED_RUN_ID,
        extraction_timestamp=UTC_TS,
    )
    assert result.preparer.role == "preparer"
    assert result.reviewer.role == "reviewer"
    assert result.preparer.date == UTC_TS
    assert result.reviewer.date == UTC_TS
