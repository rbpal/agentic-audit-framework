"""Unit tests for ``agentic_audit.layer2_narrative.silver_reader``.

Mocks the SQL connector entirely — no ``databricks-sql-connector`` dep
required for unit tests. The live-warehouse round-trip is exercised by
``tests/integration/test_layer2_silver_reader_e2e.py``
(``@pytest.mark.slow``, env-gated).

Coverage matrix:

- Happy path: 6-row DC-9 result reconstructs as a valid
  ``ExtractedEvidence`` with envelope and per-attribute checks intact.
- Happy path: 4-row DC-2 result reconstructs with the right cardinality.
- Empty rows raise ``SilverReadError`` (silver should always be
  populated for a valid triple).
- Missing envelope (NULL ``run_id`` etc.) raises ``SilverReadError``
  with a hint pointing at the migration.
- Malformed ``narrative`` JSON raises ``SilverReadError``.
- Tenacity retry: transient connection failure recovers within retries;
  permanent failure exhausts and re-raises.
- Round-trip equivalence: silver_writer's exploded row payload, fed
  back through silver_reader's parsing, yields an
  ``ExtractedEvidence`` equal in shape to the original.
- SQL pushdown: WHERE clause uses the right named parameters.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from agentic_audit.layer1_extract.silver_writer import SilverWriter
from agentic_audit.layer2_narrative.silver_reader import (
    SilverEvidenceReader,
    SilverReadError,
)
from agentic_audit.models.evidence import (
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)

UTC_TS = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)


# ---------- mock helpers -------------------------------------------------


def _silver_row(
    *,
    engagement_id: str = "alpha-pension-fund-2025",
    control_id: str = "DC-9",
    attribute_id: str = "A",
    quarter: str = "Q1",
    source_path: str = "/Volumes/.../dc9_Q1_ref.xlsx",
    source_file_hash: str = "a" * 64,
    narrative: str | None = None,
    ingested_at: datetime = UTC_TS,
    run_id: str | None = "01J0F7M5XQXM2QYAY8X8X8X8X8",
    preparer_initials: str | None = "AB",
    preparer_role: str | None = "preparer",
    preparer_date: datetime | None = UTC_TS,
    reviewer_initials: str | None = "CD",
    reviewer_role: str | None = "reviewer",
    reviewer_date: datetime | None = UTC_TS,
) -> tuple:
    """Build one silver row tuple matching SilverEvidenceReader's
    SELECT column order. Defaults give a valid populated row; pass any
    field as None to simulate pre-migration / corrupt data."""
    if narrative is None:
        narrative = json.dumps(
            {
                "status": "pass",
                "evidence_cell_refs": [f"DC9_WP!{attribute_id}1"],
                "extracted_value": {"sample": "value"},
                "notes": f"check {attribute_id}",
            },
            sort_keys=True,
        )
    return (
        engagement_id,
        control_id,
        attribute_id,
        quarter,
        source_path,
        source_file_hash,
        narrative,
        ingested_at,
        run_id,
        preparer_initials,
        preparer_role,
        preparer_date,
        reviewer_initials,
        reviewer_role,
        reviewer_date,
    )


@pytest.fixture()
def captured_calls():
    """Holds the (sql, params) of the last cursor.execute call."""
    return {}


@pytest.fixture()
def conn_factory_factory(captured_calls):
    """Returns a builder that creates a mock conn_factory and records
    every cursor.execute call into captured_calls."""

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


# ---------- happy paths --------------------------------------------------


def test_read_dc9_six_rows_reconstructs_extracted_evidence(
    conn_factory_factory, captured_calls
) -> None:
    rows = [_silver_row(attribute_id=a) for a in ("A", "B", "C", "D", "E", "F")]
    reader = SilverEvidenceReader(conn_factory_factory(rows))

    record = reader.read("alpha-pension-fund-2025", "DC-9", "Q1")

    assert isinstance(record, ExtractedEvidence)
    assert record.engagement_id == "alpha-pension-fund-2025"
    assert record.control_id == "DC-9"
    assert record.quarter == "Q1"
    assert record.run_id == "01J0F7M5XQXM2QYAY8X8X8X8X8"
    assert record.preparer.initials == "AB"
    assert record.preparer.role == "preparer"
    assert record.reviewer.initials == "CD"
    assert record.source_bronze_file_hash == "a" * 64
    assert len(record.attributes) == 6
    assert [a.attribute_id for a in record.attributes] == ["A", "B", "C", "D", "E", "F"]
    assert all(a.status == "pass" for a in record.attributes)


def test_read_dc2_four_rows_reconstructs_with_correct_cardinality(
    conn_factory_factory, captured_calls
) -> None:
    rows = [_silver_row(control_id="DC-2", attribute_id=a) for a in ("A", "B", "C", "D")]
    reader = SilverEvidenceReader(conn_factory_factory(rows))

    record = reader.read("alpha-pension-fund-2025", "DC-2", "Q1")

    assert record.control_id == "DC-2"
    assert len(record.attributes) == 4
    assert [a.attribute_id for a in record.attributes] == ["A", "B", "C", "D"]


def test_read_pushes_down_named_params(conn_factory_factory, captured_calls) -> None:
    """The WHERE filter uses three named parameters: eng, ctrl, q."""
    rows = [_silver_row(attribute_id=a) for a in ("A", "B", "C", "D", "E", "F")]
    reader = SilverEvidenceReader(conn_factory_factory(rows))
    reader.read("alpha-pension-fund-2025", "DC-9", "Q1")

    assert captured_calls["params"] == {
        "eng": "alpha-pension-fund-2025",
        "ctrl": "DC-9",
        "q": "Q1",
    }
    assert "WHERE engagement_id = %(eng)s" in captured_calls["sql"]
    assert "AND control_id    = %(ctrl)s" in captured_calls["sql"]
    assert "AND quarter       = %(q)s" in captured_calls["sql"]
    assert "ORDER BY attribute_id" in captured_calls["sql"]


# ---------- error paths --------------------------------------------------


def test_read_no_rows_raises_silver_read_error(conn_factory_factory) -> None:
    reader = SilverEvidenceReader(conn_factory_factory([]))
    with pytest.raises(SilverReadError, match="no silver rows"):
        reader.read("alpha-pension-fund-2025", "DC-2", "Q1")


def test_read_missing_envelope_raises_silver_read_error(conn_factory_factory) -> None:
    """Pre-migration row (NULL run_id / preparer / reviewer) is rejected
    with a hint that points at scripts/run_layer1.py to repopulate."""
    rows = [_silver_row(attribute_id=a, run_id=None) for a in ("A", "B", "C", "D", "E", "F")]
    reader = SilverEvidenceReader(conn_factory_factory(rows))
    with pytest.raises(SilverReadError, match="missing envelope columns"):
        reader.read("alpha-pension-fund-2025", "DC-9", "Q1")


def test_read_missing_preparer_raises_silver_read_error(conn_factory_factory) -> None:
    rows = [
        _silver_row(attribute_id=a, preparer_initials=None) for a in ("A", "B", "C", "D", "E", "F")
    ]
    reader = SilverEvidenceReader(conn_factory_factory(rows))
    with pytest.raises(SilverReadError, match="missing envelope columns"):
        reader.read("alpha-pension-fund-2025", "DC-9", "Q1")


def test_read_malformed_narrative_raises_silver_read_error(conn_factory_factory) -> None:
    rows = [_silver_row(attribute_id=a) for a in ("A", "B")]
    # Replace one row's narrative with garbage — full DC-9 set isn't
    # required to surface the JSON parse error.
    rows[0] = (*rows[0][:6], "{not valid json", *rows[0][7:])
    reader = SilverEvidenceReader(conn_factory_factory(rows))
    with pytest.raises(SilverReadError, match="malformed narrative JSON"):
        reader.read("alpha-pension-fund-2025", "DC-9", "Q1")


# ---------- retry behaviour ----------------------------------------------


def test_read_retries_transient_failure(conn_factory_factory) -> None:
    rows = [_silver_row(attribute_id=a) for a in ("A", "B", "C", "D", "E", "F")]
    factory = conn_factory_factory(rows, raise_first=2)
    reader = SilverEvidenceReader(factory)

    record = reader.read("alpha-pension-fund-2025", "DC-9", "Q1")

    assert factory.attempts["n"] == 3  # 2 failures + 1 success
    assert len(record.attributes) == 6


def test_read_exhausts_retries_and_raises(conn_factory_factory) -> None:
    factory = conn_factory_factory([], raise_first=10)  # never succeeds
    reader = SilverEvidenceReader(factory)
    with pytest.raises(ConnectionError, match="transient"):
        reader.read("alpha-pension-fund-2025", "DC-9", "Q1")
    assert factory.attempts["n"] == 3  # capped at 3 attempts


# ---------- round-trip with silver_writer --------------------------------


def _make_record(*, control_id: str = "DC-9", quarter: str = "Q1") -> ExtractedEvidence:
    ids = ["A", "B", "C", "D", "E", "F"] if control_id == "DC-9" else ["A", "B", "C", "D"]
    sheet_tag = control_id.replace("-", "")
    attrs = [
        AttributeCheck(
            control_id=control_id,  # type: ignore[arg-type]
            attribute_id=a,  # type: ignore[arg-type]
            status="pass",
            evidence_cell_refs=[f"{sheet_tag}_WP!{a}1"],
            extracted_value={"sample": f"val-{a}"},
            notes=f"check {a}",
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
        source_path=f"/bronze/{sheet_tag.lower()}_{quarter}_ref.xlsx",
    )


def test_round_trip_writer_to_reader_preserves_shape(conn_factory_factory) -> None:
    """SilverWriter.explode then SilverEvidenceReader.parse must
    reproduce the same ExtractedEvidence shape (modulo any fields the
    silver schema doesn't carry — which post-task_02a is none)."""
    original = _make_record(control_id="DC-9")

    # Simulate silver: take what silver_writer would have written,
    # convert each SilverEvidenceRow into the reader's expected tuple,
    # then feed the rows through SilverEvidenceReader.
    silver_rows = SilverWriter._explode_to_silver_rows(original)
    fake_silver_tuples = [
        (
            r.engagement_id,
            r.control_id,
            r.attribute_id,
            r.quarter,
            r.source_path,
            r.source_file_hash,
            r.narrative,
            r.ingested_at,
            r.run_id,
            r.preparer_initials,
            r.preparer_role,
            r.preparer_date,
            r.reviewer_initials,
            r.reviewer_role,
            r.reviewer_date,
        )
        for r in silver_rows
    ]
    # _explode_to_silver_rows preserves attribute order, so we don't need
    # to re-sort to match the SQL ORDER BY attribute_id.

    reader = SilverEvidenceReader(conn_factory_factory(fake_silver_tuples))
    reconstructed = reader.read("alpha-pension-fund-2025", "DC-9", "Q1")

    # All envelope fields preserved
    assert reconstructed.engagement_id == original.engagement_id
    assert reconstructed.control_id == original.control_id
    assert reconstructed.quarter == original.quarter
    assert reconstructed.run_id == original.run_id
    assert reconstructed.extraction_timestamp == original.extraction_timestamp
    assert reconstructed.preparer == original.preparer
    assert reconstructed.reviewer == original.reviewer
    assert reconstructed.source_bronze_file_hash == original.source_bronze_file_hash
    assert reconstructed.source_path == original.source_path

    # All AttributeChecks round-trip equal
    assert len(reconstructed.attributes) == len(original.attributes)
    for r_check, o_check in zip(reconstructed.attributes, original.attributes, strict=True):
        assert r_check.control_id == o_check.control_id
        assert r_check.attribute_id == o_check.attribute_id
        assert r_check.status == o_check.status
        assert r_check.evidence_cell_refs == o_check.evidence_cell_refs
        assert r_check.extracted_value == o_check.extracted_value
        assert r_check.notes == o_check.notes
