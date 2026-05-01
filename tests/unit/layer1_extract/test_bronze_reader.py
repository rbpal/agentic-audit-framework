"""Unit tests for `agentic_audit.layer1_extract.bronze_reader`.

Mocks the SQL connector entirely — no `databricks-sql-connector` dep
required for unit tests. Production wiring (which imports
`databricks.sql.connect`) is exercised by integration tests in
`tests/integration/` (marked `@pytest.mark.slow`, deferred to a later
task).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from agentic_audit.layer1_extract.bronze_reader import (
    BronzeReader,
    BronzeWorkpaperRow,
    ExtractionError,
    parse_control_quarter_from_path,
)

UTC_TS = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)


# ---------- path parsing -------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "abfss://bronze@x.dfs.core.windows.net/corpus/v2/workpapers/dc9_Q1_ref.xlsx",
            ("DC-9", "Q1"),
        ),
        (
            "abfss://bronze@x.dfs.core.windows.net/corpus/v2/workpapers/dc2_Q4_ref.xlsx",
            ("DC-2", "Q4"),
        ),
        ("/local/dc9_Q3_ref.xlsx", ("DC-9", "Q3")),
        ("DC9_q2_ref.xlsx", ("DC-9", "Q2")),  # case-insensitive
    ],
)
def test_parse_control_quarter_happy(path: str, expected: tuple[str, str]) -> None:
    assert parse_control_quarter_from_path(path) == expected


def test_parse_unknown_pattern_raises() -> None:
    with pytest.raises(ExtractionError, match="cannot parse"):
        parse_control_quarter_from_path("/no/control/info/here.xlsx")


def test_parse_unknown_control_raises() -> None:
    with pytest.raises(ExtractionError, match="unknown control_id"):
        parse_control_quarter_from_path("/path/dc7_Q1_ref.xlsx")


# ---------- mock helpers --------------------------------------------------


def _row(
    *,
    ingestion_id: int = 1,
    source_path: str = "/Volumes/audit_dev/bronze/raw_pdfs/corpus/v2/workpapers/dc9_Q1_ref.xlsx",
    file_hash: str = "a" * 64,
    engagement_id: str = "alpha-pension-fund-2025",
    sheet_name: str = "DC9_WP",
    row_index: int = 1,
    raw_data: dict[str, str] | None = None,
    ingested_at: datetime = UTC_TS,
    ingested_by: str = "smoke-ingest",
) -> tuple:
    return (
        ingestion_id,
        source_path,
        file_hash,
        engagement_id,
        sheet_name,
        row_index,
        raw_data if raw_data is not None else {"col_a": "val_a"},
        ingested_at,
        ingested_by,
    )


@pytest.fixture()
def captured_calls():
    """Holds the `(sql, params)` of the last cursor.execute call so
    individual tests can inspect SQL pushdown."""
    return {}


@pytest.fixture()
def conn_factory_factory(captured_calls):
    """Returns a builder that creates a mock conn_factory and records
    every cursor.execute call into `captured_calls`."""

    def build(rows: list[tuple], raise_first: int = 0):
        attempts = {"n": 0}

        @contextmanager
        def factory():
            attempts["n"] += 1
            if attempts["n"] <= raise_first:
                raise ConnectionError("transient warehouse hiccup")
            cur = MagicMock()
            cur.fetchall.return_value = rows

            def execute(sql, params=None):
                captured_calls["sql"] = sql
                captured_calls["params"] = params

            cur.execute.side_effect = execute
            cur.__enter__.return_value = cur
            cur.__exit__.return_value = False

            conn = MagicMock()
            conn.cursor.return_value = cur
            conn.__enter__.return_value = conn
            conn.__exit__.return_value = False
            yield conn

        factory.attempts = attempts  # type: ignore[attr-defined]
        return factory

    return build


# ---------- BronzeReader.read -------------------------------------------


def test_read_happy_path_returns_rows(conn_factory_factory, captured_calls) -> None:
    rows = [_row(ingestion_id=i, row_index=i) for i in (1, 2, 3)]
    reader = BronzeReader(conn_factory_factory(rows))

    result = reader.read("alpha-pension-fund-2025", "DC-9", "Q1")

    assert len(result) == 3
    assert all(isinstance(r, BronzeWorkpaperRow) for r in result)
    assert all(r.control_id == "DC-9" and r.quarter == "Q1" for r in result)
    assert [r.ingestion_id for r in result] == [1, 2, 3]


def test_read_empty_rows_returns_empty_list(conn_factory_factory) -> None:
    reader = BronzeReader(conn_factory_factory([]))
    result = reader.read("alpha-pension-fund-2025", "DC-2", "Q3")
    assert result == []


def test_read_pushes_down_sql_filter(conn_factory_factory, captured_calls) -> None:
    reader = BronzeReader(conn_factory_factory([]))
    reader.read("alpha-pension-fund-2025", "DC-9", "Q1")

    assert "audit_dev.bronze.workpapers_raw" in captured_calls["sql"]
    assert "engagement_id = %(eng)s" in captured_calls["sql"]
    assert "source_path LIKE %(path_like)s" in captured_calls["sql"]
    assert captured_calls["params"] == {
        "eng": "alpha-pension-fund-2025",
        "path_like": "%dc9_Q1_%",
    }


def test_read_dc2_pushes_dc2_pattern(conn_factory_factory, captured_calls) -> None:
    reader = BronzeReader(conn_factory_factory([]))
    reader.read("alpha-pension-fund-2025", "DC-2", "Q4")
    assert captured_calls["params"]["path_like"] == "%dc2_Q4_%"


# ---------- retry behavior ----------------------------------------------


def test_read_retries_transient_failure(conn_factory_factory) -> None:
    factory = conn_factory_factory([_row()], raise_first=2)
    reader = BronzeReader(factory)
    result = reader.read("alpha-pension-fund-2025", "DC-9", "Q1")
    assert len(result) == 1
    assert factory.attempts["n"] == 3  # 2 failures + 1 success  # type: ignore[attr-defined]


def test_read_exhausts_retries_and_raises(conn_factory_factory) -> None:
    factory = conn_factory_factory([_row()], raise_first=10)  # always fails
    reader = BronzeReader(factory)
    with pytest.raises(ConnectionError, match="transient"):
        reader.read("alpha-pension-fund-2025", "DC-9", "Q1")
    assert factory.attempts["n"] == 3  # capped at 3 attempts  # type: ignore[attr-defined]


# ---------- malformed input ---------------------------------------------


def test_read_row_with_unparseable_path_raises(conn_factory_factory) -> None:
    bad_row = _row(source_path="/no/control/info/here.xlsx")
    reader = BronzeReader(conn_factory_factory([bad_row]))
    with pytest.raises(ExtractionError, match="cannot parse"):
        reader.read("alpha-pension-fund-2025", "DC-9", "Q1")


def test_read_handles_null_raw_data(conn_factory_factory) -> None:
    row_with_null = (
        1,
        "/Volumes/audit_dev/bronze/raw_pdfs/corpus/v2/workpapers/dc9_Q1_ref.xlsx",
        "a" * 64,
        "alpha-pension-fund-2025",
        "DC9_WP",
        1,
        None,  # raw_data = NULL from DB
        UTC_TS,
        "smoke-ingest",
    )
    reader = BronzeReader(conn_factory_factory([row_with_null]))
    result = reader.read("alpha-pension-fund-2025", "DC-9", "Q1")
    assert result[0].raw_data == {}


# ---------- BronzeWorkpaperRow validation -------------------------------


def test_workpaper_row_rejects_invalid_control_id() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BronzeWorkpaperRow(
            ingestion_id=1,
            source_path="/x.xlsx",
            file_hash="a" * 64,
            engagement_id="eng",
            control_id="DC-99",  # type: ignore[arg-type]
            quarter="Q1",
            sheet_name="s",
            row_index=1,
            raw_data={},
            ingested_at=UTC_TS,
            ingested_by="user",
        )
