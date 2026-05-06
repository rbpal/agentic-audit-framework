"""End-to-end integration test for Layer 2 gold writer (step_05_task_06).

Marked ``@pytest.mark.slow`` and gated on ``DATABRICKS_SQL_WAREHOUSE_ID``
env var. CI's default unit-test pass skips this. Run on demand with::

    DATABRICKS_HOST=...                                              \\
    DATABRICKS_TOKEN=...                                             \\
    DATABRICKS_SQL_WAREHOUSE_ID=...                                  \\
    poetry run pytest -m slow tests/integration/test_layer2_gold_writer_e2e.py -v

Assumes ``audit_dev.gold.narratives`` has been provisioned via the
``databricks_uc`` Terraform module (``terraform apply`` after the
table addition in step_05_task_06).

What this verifies:

1. Round trip: ``write_narrative(narrative)`` then ``SELECT *`` returns
   the same row, with both array columns parsed correctly.
2. Idempotency: writing the same composite key twice leaves exactly
   one row (no duplicate, no ON CONFLICT error).
3. Prompt-version isolation: same (engagement, control, quarter,
   attribute) with two different prompt_versions creates two parallel
   rows.
4. Update semantics: same key with changed narrative_text overwrites
   the prior row's text (UPDATE SET *).

Cleanup: each test scopes its engagement_id to a test-specific
``test-task-06-...`` prefix and DELETEs its rows on teardown so reruns
stay clean.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from agentic_audit.layer2_narrative.gold_writer import (
    GOLD_NARRATIVES_TABLE,
    GoldNarrativeWriter,
)
from agentic_audit.models.narrative import AttributeNarrative

if TYPE_CHECKING:
    from collections.abc import Generator

pytestmark = pytest.mark.slow


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


@pytest.fixture()
def scoped_engagement_id(conn_factory) -> Generator[str, None, None]:
    """Yield a test-scoped engagement_id and DELETE its rows on teardown.

    The prefix ``test-task-06-`` is what the cleanup query filters on.
    Per-test UUID suffix prevents inter-test pollution if tests run in
    parallel.
    """
    engagement = f"test-task-06-{uuid.uuid4().hex[:12]}"
    yield engagement
    # Teardown: scoped DELETE
    with conn_factory() as conn, conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {GOLD_NARRATIVES_TABLE} WHERE engagement_id = %(eng)s",
            {"eng": engagement},
        )


def _make_narrative(
    *,
    engagement_id: str,
    attribute_id: str = "A",
    quarter: str = "Q1",
    prompt_version: str = "v1.0",
    narrative_text: str = "ACME Inc reconciled $1,250 in Q1 with no exceptions.",
    cited_fields: list[str] | None = None,
    fact_check_passed: bool = True,
    fact_check_issues: list[str] | None = None,
) -> AttributeNarrative:
    if cited_fields is None:
        cited_fields = ["DC9_WP!A1"]
    if fact_check_issues is None:
        fact_check_issues = []
    return AttributeNarrative(
        engagement_id=engagement_id,
        control_id="DC-9",
        attribute_id=attribute_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        source_evidence_id=f"{engagement_id}|DC-9|{quarter}|{attribute_id}",
        narrative_text=narrative_text,
        cited_fields=cited_fields,
        word_count=len(narrative_text.split()),
        prompt_version=prompt_version,
        model_deployment="gpt-4o",
        generation_run_id=uuid.uuid4().hex.upper(),
        generated_at=datetime.now(UTC),
        fact_check_passed=fact_check_passed,
        fact_check_issues=fact_check_issues,
    )


def _select_rows(conn_factory, engagement_id: str) -> list[dict[str, Any]]:
    """Return every row with the given engagement_id, sorted by
    (attribute_id, prompt_version) for deterministic assertions."""
    with conn_factory() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT engagement_id, control_id, quarter, attribute_id,
                   prompt_version, source_evidence_id, narrative_text,
                   cited_fields, word_count, model_deployment,
                   generation_run_id, generated_at,
                   fact_check_passed, fact_check_issues
            FROM   {GOLD_NARRATIVES_TABLE}
            WHERE  engagement_id = %(eng)s
            ORDER  BY attribute_id, prompt_version
            """,
            {"eng": engagement_id},
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def test_write_then_read_round_trips_one_narrative(conn_factory, scoped_engagement_id) -> None:
    """Single narrative writes successfully; SELECT returns the same
    row. Both array columns (cited_fields, fact_check_issues) parse
    correctly via the ``from_json`` cast in the MERGE."""
    narrative = _make_narrative(
        engagement_id=scoped_engagement_id,
        cited_fields=["DC9_WP!A1", "DC9_WP!A2"],
    )
    GoldNarrativeWriter(conn_factory).write_narrative(narrative)

    rows = _select_rows(conn_factory, scoped_engagement_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["engagement_id"] == scoped_engagement_id
    assert row["control_id"] == "DC-9"
    assert row["attribute_id"] == "A"
    assert row["quarter"] == "Q1"
    assert row["prompt_version"] == "v1.0"
    assert row["narrative_text"] == narrative.narrative_text
    assert list(row["cited_fields"]) == ["DC9_WP!A1", "DC9_WP!A2"]
    assert row["word_count"] == narrative.word_count
    assert row["fact_check_passed"] is True
    assert list(row["fact_check_issues"]) == []


def test_double_write_same_key_is_idempotent(conn_factory, scoped_engagement_id) -> None:
    """Writing the same composite key twice leaves exactly ONE row.
    The second MERGE matches the first row and overwrites; no
    duplicate, no ON CONFLICT error."""
    narrative = _make_narrative(engagement_id=scoped_engagement_id)
    writer = GoldNarrativeWriter(conn_factory)
    writer.write_narrative(narrative)
    writer.write_narrative(narrative)

    rows = _select_rows(conn_factory, scoped_engagement_id)
    assert len(rows) == 1


def test_write_two_versions_creates_two_rows(conn_factory, scoped_engagement_id) -> None:
    """Same (engagement, control, quarter, attribute) but different
    ``prompt_version`` → two parallel rows. This is the A/B
    comparison capability that justifies including prompt_version in
    the composite key."""
    writer = GoldNarrativeWriter(conn_factory)
    writer.write_narrative(
        _make_narrative(engagement_id=scoped_engagement_id, prompt_version="v1.0")
    )
    writer.write_narrative(
        _make_narrative(engagement_id=scoped_engagement_id, prompt_version="v1.1")
    )

    rows = _select_rows(conn_factory, scoped_engagement_id)
    assert len(rows) == 2
    versions = {row["prompt_version"] for row in rows}
    assert versions == {"v1.0", "v1.1"}


def test_write_then_write_with_changed_text_updates_row(conn_factory, scoped_engagement_id) -> None:
    """Same composite key, changed narrative_text → row reflects the
    second write. ``UPDATE SET *`` overwrites every column."""
    writer = GoldNarrativeWriter(conn_factory)
    writer.write_narrative(
        _make_narrative(
            engagement_id=scoped_engagement_id,
            narrative_text="First text — should be overwritten.",
        )
    )
    writer.write_narrative(
        _make_narrative(
            engagement_id=scoped_engagement_id,
            narrative_text="Second text — should be persisted.",
        )
    )

    rows = _select_rows(conn_factory, scoped_engagement_id)
    assert len(rows) == 1
    assert rows[0]["narrative_text"] == "Second text — should be persisted."


def test_failed_fact_check_writes_row_with_issues_array(conn_factory, scoped_engagement_id) -> None:
    """Failed fact-check is NOT a write blocker — the row lands with
    fact_check_passed=False and the issues array populated. Auditors
    filtering by ``fact_check_passed=false`` see exactly these rows."""
    issues = [
        "numeric not in evidence: '$2,500'",
        "entity not in evidence: 'Globex Corp'",
    ]
    narrative = _make_narrative(
        engagement_id=scoped_engagement_id,
        fact_check_passed=False,
        fact_check_issues=issues,
    )
    GoldNarrativeWriter(conn_factory).write_narrative(narrative)

    rows = _select_rows(conn_factory, scoped_engagement_id)
    assert len(rows) == 1
    assert rows[0]["fact_check_passed"] is False
    assert sorted(rows[0]["fact_check_issues"]) == sorted(issues)
