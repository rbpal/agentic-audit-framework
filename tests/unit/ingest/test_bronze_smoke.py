"""Unit tests for ``agentic_audit.ingest.bronze_smoke``.

Runs against the real Step-1 corpus in ``eval/gold_scenarios/`` — that's
the smoke ingest's input by definition, so we test against it directly
rather than synthesising fixtures.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic_audit.ingest.bronze_smoke import (
    TocRecord,
    WorkpaperRow,
    discover_corpus,
    extract_toc_record,
    extract_workpaper_rows,
    file_sha256,
    parse_corpus_filename,
)

CORPUS_ROOT = Path(__file__).resolve().parents[3] / "eval" / "gold_scenarios"
INGESTED_AT = datetime(2026, 1, 1, tzinfo=UTC)
ENGAGEMENT_ID = "alpha-pension-fund-2025"
INGESTED_BY = "test-runner"


def test_file_sha256_matches_hashlib(tmp_path: Path) -> None:
    payload = b"the quick brown fox\n"
    p = tmp_path / "x.bin"
    p.write_bytes(payload)
    assert file_sha256(p) == hashlib.sha256(payload).hexdigest()


def test_file_sha256_is_stable_across_calls() -> None:
    p = next((CORPUS_ROOT / "workpapers").glob("dc2_Q1_ref.xlsx"))
    assert file_sha256(p) == file_sha256(p)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("dc2_Q1_ref.xlsx", ("DC-2", "Q1")),
        ("dc9_Q4_ref.xlsx", ("DC-9", "Q4")),
        ("dc2_Q3.json", ("DC-2", "Q3")),
        ("dc9_Q2.json", ("DC-9", "Q2")),
    ],
)
def test_parse_corpus_filename_happy(name: str, expected: tuple[str, str]) -> None:
    assert parse_corpus_filename(Path(name)) == expected


def test_parse_corpus_filename_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="does not match"):
        parse_corpus_filename(Path("engagement_toc_ref.xlsx"))


def test_discover_corpus_finds_eight_workpapers_and_eight_tocs() -> None:
    workpapers, tocs = discover_corpus(CORPUS_ROOT)
    assert len(workpapers) == 8, [p.name for p in workpapers]
    assert len(tocs) == 8, [p.name for p in tocs]
    # human-readable reference TOC must be excluded — it doesn't match the regex
    assert all(p.name != "engagement_toc_ref.xlsx" for p in workpapers + tocs)


def test_extract_workpaper_rows_yields_only_nonempty() -> None:
    path = CORPUS_ROOT / "workpapers" / "dc2_Q1_ref.xlsx"
    rows = list(
        extract_workpaper_rows(
            path,
            engagement_id=ENGAGEMENT_ID,
            ingested_by=INGESTED_BY,
            ingested_at=INGESTED_AT,
        )
    )
    assert rows, "expected at least one non-empty row"
    for row in rows:
        assert isinstance(row, WorkpaperRow)
        assert row.engagement_id == ENGAGEMENT_ID
        assert row.source_path == str(path)
        assert row.file_hash == file_sha256(path)
        assert row.ingested_at == INGESTED_AT
        assert row.ingested_by == INGESTED_BY
        assert row.row_index >= 1
        assert row.raw_data, "empty rows must be filtered out"
        assert all(k.startswith("col_") for k in row.raw_data)
        assert all(isinstance(v, str) for v in row.raw_data.values())


def test_extract_workpaper_rows_all_eight_files() -> None:
    """Every Step-1 workpaper extracts at least one row — sanity smoke."""
    workpapers, _ = discover_corpus(CORPUS_ROOT)
    for path in workpapers:
        rows = list(
            extract_workpaper_rows(
                path,
                engagement_id=ENGAGEMENT_ID,
                ingested_by=INGESTED_BY,
                ingested_at=INGESTED_AT,
            )
        )
        assert rows, f"{path.name} produced zero rows"


def test_extract_toc_record_returns_typed_record() -> None:
    path = CORPUS_ROOT / "tocs" / "dc2_Q1.json"
    record = extract_toc_record(
        path,
        engagement_id=ENGAGEMENT_ID,
        ingested_by=INGESTED_BY,
        ingested_at=INGESTED_AT,
    )
    assert isinstance(record, TocRecord)
    assert record.control_id == "DC-2"
    assert record.quarter == "Q1"
    assert record.engagement_id == ENGAGEMENT_ID
    assert record.file_hash == file_sha256(path)
    # raw_json is byte-faithful — must round-trip through json.loads
    json.loads(record.raw_json)
    # and must equal the on-disk text
    assert record.raw_json == path.read_text(encoding="utf-8")


def test_extract_toc_record_rejects_corrupt_json(tmp_path: Path) -> None:
    bad = tmp_path / "dc2_Q1.json"
    bad.write_text("{not-valid-json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        extract_toc_record(
            bad,
            engagement_id=ENGAGEMENT_ID,
            ingested_by=INGESTED_BY,
            ingested_at=INGESTED_AT,
        )


def test_extract_all_eight_tocs() -> None:
    _, tocs = discover_corpus(CORPUS_ROOT)
    records = [
        extract_toc_record(
            p,
            engagement_id=ENGAGEMENT_ID,
            ingested_by=INGESTED_BY,
            ingested_at=INGESTED_AT,
        )
        for p in tocs
    ]
    # 8 unique (control_id, quarter) pairs across DC-2 + DC-9 × Q1–Q4
    pairs = {(r.control_id, r.quarter) for r in records}
    assert pairs == {
        ("DC-2", "Q1"),
        ("DC-2", "Q2"),
        ("DC-2", "Q3"),
        ("DC-2", "Q4"),
        ("DC-9", "Q1"),
        ("DC-9", "Q2"),
        ("DC-9", "Q3"),
        ("DC-9", "Q4"),
    }
