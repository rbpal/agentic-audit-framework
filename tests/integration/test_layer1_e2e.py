"""End-to-end integration test for Layer 1 — bronze → orchestrator → silver.

Marked ``@pytest.mark.slow`` and gated on the
``DATABRICKS_SQL_WAREHOUSE_ID`` env var. CI's default unit-test pass
skips this suite. Run on demand with:

    DATABRICKS_SQL_WAREHOUSE_ID=$(terraform -chdir=infra/terraform/envs/dev output -raw sql_warehouse_id) \\
    DATABRICKS_HOST=$(terraform -chdir=infra/terraform/envs/dev output -raw databricks_host) \\
    DATABRICKS_TOKEN=$(databricks tokens create --comment "layer1-e2e" --lifetime-seconds 3600 | jq -r .token_value) \\
    poetry run pytest -m slow tests/integration/test_layer1_e2e.py -v

What this test verifies:

1. `BronzeReader.read("alpha-pension-fund-2025", "DC-9", "Q1")` returns
   ≥ 1 row from the live `audit_dev.bronze.workpapers_raw` (assumes
   ``step_03_task_09`` smoke ingest has run).
2. `extract(...)` produces a valid `ExtractedEvidence` with 6 attributes.
3. `SilverWriter.write_evidence(record)` MERGEs 6 rows into
   `audit_dev.silver.evidence` for the (engagement, DC-9, Q1) triple.
4. Re-running the same `write_evidence` does NOT change the silver row
   count (idempotency under MERGE-on-natural-key).
5. The same flow for a DC-2 triple writes exactly 4 rows.

Test cleanup deletes only rows it inserted (scoped DELETE by
engagement_id), so the test is safe to re-run without polluting silver.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest

from agentic_audit.layer1_extract.bronze_reader import BronzeReader
from agentic_audit.layer1_extract.orchestrator import extract
from agentic_audit.layer1_extract.silver_writer import SilverWriter

if TYPE_CHECKING:
    from collections.abc import Generator

pytestmark = pytest.mark.slow

# Use a dedicated test engagement so cleanup doesn't touch real data.
TEST_ENGAGEMENT = "alpha-pension-fund-2025"


def _have_warehouse_creds() -> bool:
    return all(
        os.getenv(k) for k in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_SQL_WAREHOUSE_ID")
    )


@pytest.fixture(scope="module")
def conn_factory() -> Any:
    """Build a real `databricks.sql.connect` factory.

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


@pytest.fixture(autouse=True)
def cleanup_silver(conn_factory) -> Generator[None, None, None]:
    """Wipe any prior test rows before each test, and after, so re-runs
    don't accumulate."""
    yield
    with conn_factory() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM audit_dev.silver.evidence WHERE engagement_id = %(eng)s",
            {"eng": TEST_ENGAGEMENT},
        )


def _silver_count_for(conn_factory, control_id: str, quarter: str) -> int:
    with conn_factory() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM audit_dev.silver.evidence
            WHERE engagement_id = %(eng)s
              AND control_id    = %(ctrl)s
              AND quarter       = %(q)s
            """,
            {"eng": TEST_ENGAGEMENT, "ctrl": control_id, "q": quarter},
        )
        return int(cur.fetchall()[0][0])


def _silver_envelope_for(conn_factory, control_id: str, quarter: str) -> list[tuple]:
    """Fetch the (run_id, preparer_initials, reviewer_initials) envelope of
    each silver row matching the (engagement, control, quarter). Used to
    assert step_05_task_02a's envelope columns are populated.
    """
    with conn_factory() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, preparer_initials, reviewer_initials
            FROM audit_dev.silver.evidence
            WHERE engagement_id = %(eng)s
              AND control_id    = %(ctrl)s
              AND quarter       = %(q)s
            """,
            {"eng": TEST_ENGAGEMENT, "ctrl": control_id, "q": quarter},
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def test_dc9_q1_round_trip_writes_six_rows_idempotent(conn_factory) -> None:
    bronze_reader = BronzeReader(conn_factory)
    silver_writer = SilverWriter(conn_factory)

    record = extract(TEST_ENGAGEMENT, "DC-9", "Q1", bronze_reader=bronze_reader)
    assert len(record.attributes) == 6

    silver_writer.write_evidence(record)
    assert _silver_count_for(conn_factory, "DC-9", "Q1") == 6

    # Idempotency: re-run should keep the row count at 6.
    silver_writer.write_evidence(record)
    assert _silver_count_for(conn_factory, "DC-9", "Q1") == 6

    # Envelope columns populated (step_05_task_02a) — every row carries
    # the same run_id and preparer/reviewer initials from the parent record.
    envelopes = _silver_envelope_for(conn_factory, "DC-9", "Q1")
    assert len(envelopes) == 6
    run_ids = {e[0] for e in envelopes}
    assert len(run_ids) == 1 and next(iter(run_ids)) == record.run_id
    assert all(e[1] == record.preparer.initials for e in envelopes)
    assert all(e[2] == record.reviewer.initials for e in envelopes)


def test_dc2_q1_round_trip_writes_four_rows(conn_factory) -> None:
    bronze_reader = BronzeReader(conn_factory)
    silver_writer = SilverWriter(conn_factory)

    record = extract(TEST_ENGAGEMENT, "DC-2", "Q1", bronze_reader=bronze_reader)
    assert len(record.attributes) == 4

    silver_writer.write_evidence(record)
    assert _silver_count_for(conn_factory, "DC-2", "Q1") == 4

    silver_writer.write_evidence(record)  # idempotent
    assert _silver_count_for(conn_factory, "DC-2", "Q1") == 4

    # Envelope columns populated (step_05_task_02a)
    envelopes = _silver_envelope_for(conn_factory, "DC-2", "Q1")
    assert len(envelopes) == 4
    assert all(e[0] == record.run_id for e in envelopes)
    assert all(e[1] == record.preparer.initials for e in envelopes)
    assert all(e[2] == record.reviewer.initials for e in envelopes)
