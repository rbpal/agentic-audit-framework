"""End-to-end integration test for Layer 2 silver reader (step_05_task_02b).

Marked ``@pytest.mark.slow`` and gated on ``DATABRICKS_SQL_WAREHOUSE_ID``
env var. CI's default unit-test pass skips this. Run on demand with::

    DATABRICKS_HOST=...                                              \\
    DATABRICKS_TOKEN=...                                             \\
    DATABRICKS_SQL_WAREHOUSE_ID=...                                  \\
    poetry run pytest -m slow tests/integration/test_layer2_silver_reader_e2e.py -v

Assumes silver has been populated by ``scripts/run_layer1.py`` post
step_05_task_02a — every row should carry the envelope columns
(``run_id`` / ``preparer_*`` / ``reviewer_*``).

What this verifies:

1. ``SilverEvidenceReader.read("alpha-pension-fund-2025", "DC-9", "Q1")``
   returns a fully-validated ``ExtractedEvidence`` with 6 attributes.
2. Same for DC-2 with 4 attributes.
3. Round trip: ``extract(...)`` → ``SilverWriter.write_evidence(...)``
   → ``SilverEvidenceReader.read(...)`` produces the same envelope and
   the same per-attribute checks as the original Layer 1 output.
4. Reading a triple that doesn't exist raises ``SilverReadError``.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest

from agentic_audit.layer1_extract.bronze_reader import BronzeReader
from agentic_audit.layer1_extract.orchestrator import extract
from agentic_audit.layer1_extract.silver_writer import SilverWriter
from agentic_audit.layer2_narrative.silver_reader import (
    SilverEvidenceReader,
    SilverReadError,
)

if TYPE_CHECKING:
    from collections.abc import Generator

pytestmark = pytest.mark.slow

# Use the same engagement as test_layer1_e2e so cleanup logic is unified.
TEST_ENGAGEMENT = "alpha-pension-fund-2025"


def _have_warehouse_creds() -> bool:
    return all(
        os.getenv(k) for k in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_SQL_WAREHOUSE_ID")
    )


@pytest.fixture(scope="module")
def conn_factory() -> Any:
    """Build a real ``databricks.sql.connect`` factory.

    Skipped if creds are absent — the test suite is opt-in via env vars.
    """
    if not _have_warehouse_creds():
        pytest.skip(
            "DATABRICKS_HOST / DATABRICKS_TOKEN / DATABRICKS_SQL_WAREHOUSE_ID "
            "not set; skipping live integration test"
        )
    from databricks import sql as dbsql  # type: ignore[import-not-found]

    host = os.environ["DATABRICKS_HOST"]
    if not host.startswith("https://"):
        host = f"https://{host}"
    http_path = f"/sql/1.0/warehouses/{os.environ['DATABRICKS_SQL_WAREHOUSE_ID']}"
    token = os.environ["DATABRICKS_TOKEN"]

    @contextmanager
    def factory() -> Generator[Any, None, None]:
        conn = dbsql.connect(
            server_hostname=host.removeprefix("https://"),
            http_path=http_path,
            access_token=token,
        )
        try:
            yield conn
        finally:
            conn.close()

    return factory


def test_read_dc9_q1_returns_six_attributes(conn_factory) -> None:
    reader = SilverEvidenceReader(conn_factory)
    record = reader.read(TEST_ENGAGEMENT, "DC-9", "Q1")

    assert record.engagement_id == TEST_ENGAGEMENT
    assert record.control_id == "DC-9"
    assert record.quarter == "Q1"
    assert len(record.attributes) == 6
    assert sorted(a.attribute_id for a in record.attributes) == ["A", "B", "C", "D", "E", "F"]
    # Envelope must be populated post-task_02a migration
    assert record.run_id  # non-empty
    assert record.preparer.initials  # non-empty
    assert record.reviewer.initials  # non-empty


def test_read_dc2_q3_returns_four_attributes(conn_factory) -> None:
    reader = SilverEvidenceReader(conn_factory)
    record = reader.read(TEST_ENGAGEMENT, "DC-2", "Q3")

    assert record.control_id == "DC-2"
    assert record.quarter == "Q3"
    assert len(record.attributes) == 4
    assert sorted(a.attribute_id for a in record.attributes) == ["A", "B", "C", "D"]


def test_read_unknown_triple_raises_silver_read_error(conn_factory) -> None:
    """A triple that has no silver rows (different engagement) raises
    SilverReadError — the reader is loud about missing data rather
    than returning an empty / sentinel ExtractedEvidence."""
    reader = SilverEvidenceReader(conn_factory)
    with pytest.raises(SilverReadError, match="no silver rows"):
        reader.read("does-not-exist-engagement", "DC-9", "Q1")


def test_round_trip_extract_write_read_preserves_envelope(conn_factory) -> None:
    """Extract from bronze, write to silver, read back. Envelope and
    every AttributeCheck round-trip equal."""
    bronze_reader = BronzeReader(conn_factory)
    silver_writer = SilverWriter(conn_factory)
    silver_reader = SilverEvidenceReader(conn_factory)

    original = extract(TEST_ENGAGEMENT, "DC-9", "Q2", bronze_reader=bronze_reader)
    silver_writer.write_evidence(original)
    reconstructed = silver_reader.read(TEST_ENGAGEMENT, "DC-9", "Q2")

    # Envelope round-trips
    assert reconstructed.engagement_id == original.engagement_id
    assert reconstructed.control_id == original.control_id
    assert reconstructed.quarter == original.quarter
    assert reconstructed.run_id == original.run_id
    assert reconstructed.preparer == original.preparer
    assert reconstructed.reviewer == original.reviewer
    assert reconstructed.source_bronze_file_hash == original.source_bronze_file_hash
    assert reconstructed.source_path == original.source_path

    # Per-attribute checks round-trip equal (order may differ due to
    # ORDER BY attribute_id; sort both sides by attribute_id first).
    orig_by_attr = {a.attribute_id: a for a in original.attributes}
    recon_by_attr = {a.attribute_id: a for a in reconstructed.attributes}
    assert set(orig_by_attr) == set(recon_by_attr)
    for attr_id in orig_by_attr:
        o = orig_by_attr[attr_id]
        r = recon_by_attr[attr_id]
        assert r.control_id == o.control_id
        assert r.status == o.status
        assert r.evidence_cell_refs == o.evidence_cell_refs
        assert r.extracted_value == o.extracted_value
        assert r.notes == o.notes
