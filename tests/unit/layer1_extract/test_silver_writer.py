"""Unit tests for `agentic_audit.layer1_extract.silver_writer`.

Mocks the SQL connector entirely — no `databricks-sql-connector` dep
required for unit tests. Production wiring (which imports
`databricks.sql.connect`) is exercised by the integration test in
`tests/integration/test_layer1_e2e.py` (marked `@pytest.mark.slow`).

Coverage matrix:

- `_explode_to_silver_rows` produces 4 rows for DC-2, 6 for DC-9 with
  attribute_id populated correctly per row.
- `evidence_id` is stable for the same natural key — re-running the
  writer for the same `(engagement, control, attribute, quarter)`
  triple emits the same id.
- The staged-view + MERGE SQL contains the expected join keys.
- Tenacity retry behavior matches the bronze reader: transient failure
  recovers within retries; permanent failure exhausts and re-raises.
- `narrative` round-trips as JSON.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from agentic_audit.layer1_extract.silver_writer import (
    SilverEvidenceRow,
    SilverWriter,
    _attribute_check_to_narrative,
    _evidence_id,
)
from agentic_audit.models.evidence import (
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)

UTC_TS = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)


def _make_check(
    attribute_id: str, *, control_id: str = "DC-9", status: str = "pass"
) -> AttributeCheck:
    sheet_tag = control_id.replace("-", "")  # 'DC-9' → 'DC9'
    return AttributeCheck(
        control_id=control_id,  # type: ignore[arg-type]
        attribute_id=attribute_id,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        evidence_cell_refs=[f"{sheet_tag}_WP!{attribute_id}1"],
        extracted_value={"sample": "value"},
        notes=f"check {attribute_id}",
    )


def _make_record(
    *,
    control_id: str = "DC-9",
    quarter: str = "Q1",
    attributes: list[AttributeCheck] | None = None,
) -> ExtractedEvidence:
    if attributes is None:
        ids = ["A", "B", "C", "D", "E", "F"] if control_id == "DC-9" else ["A", "B", "C", "D"]
        attributes = [_make_check(a, control_id=control_id) for a in ids]
    return ExtractedEvidence(
        engagement_id="alpha-pension-fund-2025",
        control_id=control_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        run_id="01J0F7M5XQXM2QYAY8X8X8X8X8",
        extraction_timestamp=UTC_TS,
        preparer=SignOff(initials="AB", role="preparer", date=UTC_TS),
        reviewer=SignOff(initials="CD", role="reviewer", date=UTC_TS),
        attributes=attributes,
        source_bronze_file_hash="a" * 64,
        source_path="/Volumes/audit_dev/bronze/raw_pdfs/corpus/v2/workpapers/dc9_Q1_ref.xlsx",
    )


# ---------- _evidence_id stability ---------------------------------------


def test_evidence_id_is_stable_for_same_natural_key() -> None:
    a = _evidence_id("eng", "DC-9", "A", "Q1")
    b = _evidence_id("eng", "DC-9", "A", "Q1")
    assert a == b


def test_evidence_id_differs_when_any_key_component_differs() -> None:
    base = _evidence_id("eng", "DC-9", "A", "Q1")
    assert _evidence_id("eng2", "DC-9", "A", "Q1") != base
    assert _evidence_id("eng", "DC-2", "A", "Q1") != base
    assert _evidence_id("eng", "DC-9", "B", "Q1") != base
    assert _evidence_id("eng", "DC-9", "A", "Q2") != base


def test_evidence_id_fits_in_signed_bigint() -> None:
    """Signed bigint max = 2^63 - 1. Sign bit is masked off, so result is positive."""
    eid = _evidence_id("eng", "DC-9", "A", "Q1")
    assert 0 <= eid < (1 << 63)


# ---------- _attribute_check_to_narrative --------------------------------


def test_narrative_round_trips_as_json() -> None:
    check = _make_check("A", control_id="DC-9")
    narrative = _attribute_check_to_narrative(check)
    parsed = json.loads(narrative)
    assert parsed["status"] == "pass"
    assert parsed["evidence_cell_refs"] == ["DC9_WP!A1"]
    assert parsed["extracted_value"] == {"sample": "value"}
    assert parsed["notes"] == "check A"


def test_narrative_handles_n_a_status() -> None:
    check = AttributeCheck(
        control_id="DC-9",
        attribute_id="D",
        status="n/a",
        evidence_cell_refs=[],
        notes="Q1 has no prior period",
    )
    narrative = _attribute_check_to_narrative(check)
    parsed = json.loads(narrative)
    assert parsed["status"] == "n/a"
    assert parsed["evidence_cell_refs"] == []
    assert parsed["extracted_value"] is None


# ---------- _explode_to_silver_rows --------------------------------------


def test_explode_dc9_record_produces_six_rows() -> None:
    record = _make_record(control_id="DC-9")
    rows = SilverWriter._explode_to_silver_rows(record)
    assert len(rows) == 6
    assert [r.attribute_id for r in rows] == ["A", "B", "C", "D", "E", "F"]
    assert all(isinstance(r, SilverEvidenceRow) for r in rows)
    assert all(r.engagement_id == "alpha-pension-fund-2025" for r in rows)
    assert all(r.control_id == "DC-9" for r in rows)
    assert all(r.quarter == "Q1" for r in rows)
    assert all(r.evidence_type == "workpaper-row" for r in rows)
    assert all(r.source_file_hash == "a" * 64 for r in rows)


def test_explode_dc2_record_produces_four_rows() -> None:
    record = _make_record(control_id="DC-2")
    rows = SilverWriter._explode_to_silver_rows(record)
    assert len(rows) == 4
    assert [r.attribute_id for r in rows] == ["A", "B", "C", "D"]


def test_explode_evidence_ids_are_unique_within_a_record() -> None:
    """Each (control, attribute, quarter) triple has its own stable id."""
    record = _make_record(control_id="DC-9")
    rows = SilverWriter._explode_to_silver_rows(record)
    ids = [r.evidence_id for r in rows]
    assert len(set(ids)) == 6


def test_explode_evidence_ids_match_helper() -> None:
    record = _make_record(control_id="DC-9")
    rows = SilverWriter._explode_to_silver_rows(record)
    for r in rows:
        assert r.evidence_id == _evidence_id(
            r.engagement_id, r.control_id, r.attribute_id, r.quarter
        )


def test_explode_narrative_carries_attribute_check_payload() -> None:
    record = _make_record(control_id="DC-2")
    rows = SilverWriter._explode_to_silver_rows(record)
    parsed = json.loads(rows[0].narrative)
    assert parsed["status"] == "pass"
    assert parsed["evidence_cell_refs"] == ["DC2_WP!A1"]


# ---------- envelope columns (step_05_task_02a) --------------------------


def test_explode_propagates_run_id_to_every_row() -> None:
    """Every silver row produced from one ExtractedEvidence shares the same
    run_id. Downstream readers reconstruct ExtractedEvidence from any row's
    envelope, so the redundancy is intentional and required."""
    record = _make_record(control_id="DC-9")
    rows = SilverWriter._explode_to_silver_rows(record)
    assert all(r.run_id == "01J0F7M5XQXM2QYAY8X8X8X8X8" for r in rows)


def test_explode_propagates_preparer_signoff_to_every_row() -> None:
    record = _make_record(control_id="DC-9")
    rows = SilverWriter._explode_to_silver_rows(record)
    assert all(r.preparer_initials == "AB" for r in rows)
    assert all(r.preparer_role == "preparer" for r in rows)
    assert all(r.preparer_date == UTC_TS for r in rows)


def test_explode_propagates_reviewer_signoff_to_every_row() -> None:
    record = _make_record(control_id="DC-2")
    rows = SilverWriter._explode_to_silver_rows(record)
    assert all(r.reviewer_initials == "CD" for r in rows)
    assert all(r.reviewer_role == "reviewer" for r in rows)
    assert all(r.reviewer_date == UTC_TS for r in rows)


def test_silver_evidence_row_rejects_missing_envelope() -> None:
    """SilverEvidenceRow's envelope fields are required at write time —
    Layer 1 always knows them. The Delta column is nullable for back-compat
    only; the Python writer never produces a row without them."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SilverEvidenceRow(
            evidence_id=1,
            engagement_id="x",
            control_id="DC-9",
            attribute_id="A",
            quarter="Q1",
            source_path="/p",
            source_file_hash="h",
            evidence_type="workpaper-row",
            narrative="{}",
            ingested_at=UTC_TS,
            # Envelope fields omitted — should fail
        )


# ---------- _build_values_clause -----------------------------------------


def test_build_values_clause_returns_one_tuple_per_row() -> None:
    record = _make_record(control_id="DC-9")
    rows = SilverWriter._explode_to_silver_rows(record)
    params, clause = SilverWriter._build_values_clause(rows)
    # Count row-tuple opens — each row starts with `(%(`. There's one per row.
    assert clause.count("(%(") == 6
    # 6 rows × 17 columns (10 original + 7 envelope per task_02a) = 102 params
    assert len(params) == 102
    assert "evidence_id_0" in params
    assert "narrative_5" in params
    assert "%(evidence_id_0)s" in clause
    # Envelope columns present
    assert "run_id_0" in params
    assert "preparer_initials_0" in params
    assert "reviewer_date_5" in params


# ---------- SilverWriter.write_evidence — happy path ---------------------


@pytest.fixture()
def captured_calls() -> dict[str, list]:
    """Holds the (sql, params) of every cursor.execute call."""
    return {"executes": []}


@pytest.fixture()
def conn_factory_factory(captured_calls):
    """Builds a mock conn_factory that records every execute call."""

    def build(*, raise_first: int = 0):
        attempts = {"n": 0}

        @contextmanager
        def factory():
            attempts["n"] += 1
            if attempts["n"] <= raise_first:
                raise ConnectionError("transient warehouse hiccup")
            cur = MagicMock()

            def execute(sql, params=None):
                captured_calls["executes"].append((sql, params))

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


def test_write_evidence_dc9_executes_staged_view_and_merge(
    conn_factory_factory, captured_calls
) -> None:
    record = _make_record(control_id="DC-9")
    SilverWriter(conn_factory_factory()).write_evidence(record)

    assert len(captured_calls["executes"]) == 2
    staged_sql, staged_params = captured_calls["executes"][0]
    merge_sql, merge_params = captured_calls["executes"][1]

    # Staged view SQL
    assert "CREATE OR REPLACE TEMPORARY VIEW _silver_staged" in staged_sql
    assert "VALUES" in staged_sql
    # Envelope columns are part of the staged view's column list
    assert "run_id" in staged_sql
    assert "preparer_initials" in staged_sql
    assert "reviewer_date" in staged_sql
    # 6 rows × 17 cols (10 original + 7 envelope per task_02a) = 102 params
    assert len(staged_params) == 102
    # Merge SQL
    assert "MERGE INTO audit_dev.silver.evidence" in merge_sql
    assert "t.engagement_id = s.engagement_id" in merge_sql
    assert "t.control_id    = s.control_id" in merge_sql
    assert "t.attribute_id  = s.attribute_id" in merge_sql
    assert "t.quarter       = s.quarter" in merge_sql
    assert "WHEN MATCHED THEN UPDATE SET *" in merge_sql
    assert "WHEN NOT MATCHED THEN INSERT *" in merge_sql
    assert merge_params is None  # MERGE has no parameters


def test_write_evidence_dc2_writes_four_row_payload(conn_factory_factory, captured_calls) -> None:
    record = _make_record(control_id="DC-2")
    SilverWriter(conn_factory_factory()).write_evidence(record)

    staged_params = captured_calls["executes"][0][1]
    # 4 rows × 17 cols (10 original + 7 envelope per task_02a)
    assert len(staged_params) == 68
    # All 4 attribute IDs present
    attrs = sorted(v for k, v in staged_params.items() if k.startswith("attribute_id_"))
    assert attrs == ["A", "B", "C", "D"]


# ---------- retry behavior ----------------------------------------------


def test_write_evidence_retries_transient_failure(conn_factory_factory) -> None:
    record = _make_record(control_id="DC-9")
    factory = conn_factory_factory(raise_first=2)
    SilverWriter(factory).write_evidence(record)
    assert factory.attempts["n"] == 3  # 2 failures + 1 success


def test_write_evidence_exhausts_retries_and_raises(conn_factory_factory) -> None:
    record = _make_record(control_id="DC-9")
    factory = conn_factory_factory(raise_first=10)  # never succeeds
    writer = SilverWriter(factory)
    with pytest.raises(ConnectionError, match="transient"):
        writer.write_evidence(record)
    assert factory.attempts["n"] == 3  # capped at 3 attempts
