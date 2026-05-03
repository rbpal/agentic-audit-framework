"""Unit tests for `scripts/run_layer1.py`.

The live-warehouse runner is not exercised in CI (no warehouse creds);
these tests verify scenario parsing + sweep loop wiring (extract +
write_evidence are called exactly once per scenario, with the right
args) using mocked bronze_reader and silver_writer.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# `scripts/` isn't a package on PYTHONPATH by default; add it explicitly.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from run_layer1 import (  # type: ignore[import-not-found]  # noqa: E402
    SCENARIOS,
    parse_scenarios,
    run_sweep,
)

from agentic_audit.models.evidence import (  # noqa: E402
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)

UTC_TS = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)


# ---------- scenario list pinned ----------------------------------------


def test_default_scenarios_pinned_to_eight() -> None:
    """Sweep size = 8 per the v2 corpus (DC-2 + DC-9 × Q1–Q4)."""
    assert len(SCENARIOS) == 8
    assert set(SCENARIOS) == {
        ("DC-2", "Q1"),
        ("DC-2", "Q2"),
        ("DC-2", "Q3"),
        ("DC-2", "Q4"),
        ("DC-9", "Q1"),
        ("DC-9", "Q2"),
        ("DC-9", "Q3"),
        ("DC-9", "Q4"),
    }


# ---------- parse_scenarios ---------------------------------------------


def test_parse_scenarios_none_returns_default_sweep() -> None:
    assert parse_scenarios(None) == SCENARIOS


def test_parse_scenarios_empty_string_returns_default_sweep() -> None:
    assert parse_scenarios("") == SCENARIOS


def test_parse_scenarios_single_tag() -> None:
    assert parse_scenarios("dc9_q1") == (("DC-9", "Q1"),)


def test_parse_scenarios_multiple_tags() -> None:
    assert parse_scenarios("dc2_q3,dc9_q1") == (("DC-2", "Q3"), ("DC-9", "Q1"))


def test_parse_scenarios_handles_whitespace_and_uppercase_quarter() -> None:
    assert parse_scenarios(" dc9_Q4 , dc2_Q2 ") == (("DC-9", "Q4"), ("DC-2", "Q2"))


def test_parse_scenarios_rejects_bad_format() -> None:
    with pytest.raises(ValueError, match="bad scenario tag"):
        parse_scenarios("dc9q1")  # missing underscore


def test_parse_scenarios_rejects_unknown_control() -> None:
    with pytest.raises(ValueError, match="bad scenario tag"):
        parse_scenarios("dc5_q1")


def test_parse_scenarios_rejects_unknown_quarter() -> None:
    with pytest.raises(ValueError, match="bad scenario tag"):
        parse_scenarios("dc9_q5")


# ---------- run_sweep ---------------------------------------------------


def _fake_record(control_id: str, quarter: str) -> ExtractedEvidence:
    ids = ["A", "B", "C", "D", "E", "F"] if control_id == "DC-9" else ["A", "B", "C", "D"]
    attrs = [
        AttributeCheck(
            control_id=control_id,  # type: ignore[arg-type]
            attribute_id=a,  # type: ignore[arg-type]
            status="pass",
            evidence_cell_refs=[],
        )
        for a in ids
    ]
    return ExtractedEvidence(
        engagement_id="alpha-pension-fund-2025",
        control_id=control_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        run_id="01J0F7M5XQXM2QYAY8X8X8X8X8",
        extraction_timestamp=UTC_TS,
        preparer=SignOff(initials="AB", role="preparer", date=UTC_TS),
        reviewer=SignOff(initials="CD", role="reviewer", date=UTC_TS),
        attributes=attrs,
        source_bronze_file_hash="a" * 64,
        source_path=f"/bronze/dc{control_id.split('-')[1]}_{quarter}_ref.xlsx",
    )


def test_run_sweep_calls_extract_and_write_once_per_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sweep iterates scenarios and invokes extract → write_evidence
    exactly once per scenario. Returns total silver-row count."""
    bronze_reader = MagicMock()
    silver_writer = MagicMock()

    extract_calls: list[tuple[str, str, str]] = []

    def fake_extract(eng, ctrl, q, *, bronze_reader=None):
        extract_calls.append((eng, ctrl, q))
        return _fake_record(ctrl, q)

    monkeypatch.setattr("run_layer1.extract", fake_extract)

    scenarios = (("DC-2", "Q1"), ("DC-9", "Q3"))
    total_rows = run_sweep(
        engagement_id="alpha-pension-fund-2025",
        bronze_reader=bronze_reader,
        silver_writer=silver_writer,
        scenarios=scenarios,
    )

    # extract was called once per scenario with the right args
    assert extract_calls == [
        ("alpha-pension-fund-2025", "DC-2", "Q1"),
        ("alpha-pension-fund-2025", "DC-9", "Q3"),
    ]
    # silver_writer.write_evidence was called once per scenario
    assert silver_writer.write_evidence.call_count == 2
    # Total rows: 4 (DC-2) + 6 (DC-9) = 10
    assert total_rows == 10


def test_run_sweep_default_eight_scenarios_writes_forty_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full v2 sweep: 4×4 (DC-2) + 4×6 (DC-9) = 16 + 24 = 40 silver rows."""
    bronze_reader = MagicMock()
    silver_writer = MagicMock()

    monkeypatch.setattr(
        "run_layer1.extract", lambda eng, c, q, *, bronze_reader=None: _fake_record(c, q)
    )

    total_rows = run_sweep(
        engagement_id="alpha-pension-fund-2025",
        bronze_reader=bronze_reader,
        silver_writer=silver_writer,
    )
    assert silver_writer.write_evidence.call_count == 8
    assert total_rows == 40
