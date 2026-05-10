"""End-to-end integration test for Layer 2 judge outcomes writer
(step_06_task_04).

Marked ``@pytest.mark.slow`` and gated on ``DATABRICKS_SQL_WAREHOUSE_ID``
env var. CI's default unit-test pass skips this. Run on demand with::

    DATABRICKS_HOST=...                                              \\
    DATABRICKS_TOKEN=...                                             \\
    DATABRICKS_SQL_WAREHOUSE_ID=...                                  \\
    poetry run pytest -m slow tests/integration/test_layer2_judge_outcomes_writer_e2e.py -v

Assumes ``audit_dev.gold.judge_outcomes`` has been provisioned via the
``databricks_uc`` Terraform module (``terraform apply`` after the
table addition in step_06_task_04).

What this verifies:

1. **Round trip** — ``write_judge_outcome(outcome)`` then
   ``SELECT *`` returns the same row, with ``cited_evidence_fields``
   parsed correctly via the ``from_json`` cast.
2. **Empty cited fields** — ``verdict='uncertain'`` with an empty
   ``cited_evidence_fields`` round-trips (the JSON ``[]`` cast lands as
   an empty array, not NULL).
3. **Append-only contract** — two writes with the same
   ``narrative_run_id`` but different ``judge_run_id`` produce TWO
   rows, not one. This is the deliberate departure from
   ``GoldNarrativeWriter``'s MERGE — divergence-over-time is the asset.

Cleanup: each test scopes its ``judge_run_id`` to a test-specific
``test-step-06-task-04-...`` prefix and DELETEs its rows on teardown
so reruns stay clean.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from agentic_audit.layer2_narrative.judge_outcomes_writer import (
    GOLD_JUDGE_OUTCOMES_TABLE,
    JudgeOutcomesWriter,
)
from agentic_audit.models.judge import JudgeOutcomeRow

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
def scoped_judge_run_id(conn_factory) -> Generator[str, None, None]:
    """Yield a test-scoped ``judge_run_id`` and DELETE its rows on
    teardown.

    The prefix ``test-step-06-task-04-`` is what the cleanup query
    filters on. Per-test UUID suffix prevents inter-test pollution if
    tests run in parallel.
    """
    judge_run_id = f"test-step-06-task-04-{uuid.uuid4().hex[:12]}"
    yield judge_run_id
    with conn_factory() as conn, conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {GOLD_JUDGE_OUTCOMES_TABLE} WHERE judge_run_id = %(jrid)s",
            {"jrid": judge_run_id},
        )


def _make_outcome(
    *,
    judge_run_id: str,
    narrative_run_id: str | None = None,
    attribute_id: str = "A",
    quarter: str = "Q1",
    judge_verdict: str = "pass",
    judge_confidence: float = 0.92,
    judge_reasoning: str = "Preparer signature dated within Q+5 days; evidence supports the claim.",
    cited_evidence_fields: list[str] | None = None,
    judge_status: str = "ok",
    gold_expected_verdict: str = "pass",
    fact_check_verdict: str = "pass",
) -> JudgeOutcomeRow:
    if cited_evidence_fields is None:
        cited_evidence_fields = ["DC9_WP!A1", "DC9_WP!A2"]
    if narrative_run_id is None:
        narrative_run_id = uuid.uuid4().hex.upper()
    return JudgeOutcomeRow(
        judge_run_id=judge_run_id,
        narrative_run_id=narrative_run_id,
        engagement_id="alpha-pension-fund-2025",
        control_id="DC-9",
        attribute_id=attribute_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        judge_verdict=judge_verdict,  # type: ignore[arg-type]
        judge_confidence=judge_confidence,
        judge_reasoning=judge_reasoning,
        cited_evidence_fields=cited_evidence_fields,
        judge_status=judge_status,  # type: ignore[arg-type]
        gold_expected_verdict=gold_expected_verdict,
        fact_check_verdict=fact_check_verdict,  # type: ignore[arg-type]
        prompt_version="judge_v1.0",
        model_deployment="gpt-4o",
        evaluated_at=datetime.now(UTC),
    )


def _select_rows(conn_factory, judge_run_id: str) -> list[dict[str, Any]]:
    """Return every row with the given ``judge_run_id``, sorted by
    ``narrative_run_id`` for deterministic assertions."""
    with conn_factory() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT judge_run_id, narrative_run_id, engagement_id,
                   control_id, attribute_id, quarter,
                   judge_verdict, judge_confidence, judge_reasoning,
                   cited_evidence_fields, judge_status,
                   gold_expected_verdict, fact_check_verdict,
                   prompt_version, model_deployment, evaluated_at
            FROM   {GOLD_JUDGE_OUTCOMES_TABLE}
            WHERE  judge_run_id = %(jrid)s
            ORDER  BY narrative_run_id
            """,
            {"jrid": judge_run_id},
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def test_write_then_read_round_trips_full_judge_outcome_row(
    conn_factory, scoped_judge_run_id
) -> None:
    """One ``JudgeOutcomeRow`` writes successfully; SELECT returns the
    same row. ``cited_evidence_fields`` parses correctly via the
    ``from_json`` cast in the INSERT statement.

    Pins every column the unit tests pin in mocked form — but here
    the SQL actually runs against ``audit_dev.gold.judge_outcomes``,
    catching column-order / type-cast issues the unit tests can't see.
    """
    outcome = _make_outcome(
        judge_run_id=scoped_judge_run_id,
        cited_evidence_fields=["DC9_WP!A1", "DC9_WP!A2", "DC9_WP!preparer_signoff"],
    )
    JudgeOutcomesWriter(conn_factory).write_judge_outcome(outcome)

    rows = _select_rows(conn_factory, scoped_judge_run_id)
    assert len(rows) == 1
    row = rows[0]

    assert row["judge_run_id"] == scoped_judge_run_id
    assert row["narrative_run_id"] == outcome.narrative_run_id
    assert row["engagement_id"] == "alpha-pension-fund-2025"
    assert row["control_id"] == "DC-9"
    assert row["attribute_id"] == "A"
    assert row["quarter"] == "Q1"
    assert row["judge_verdict"] == "pass"
    assert row["judge_confidence"] == pytest.approx(0.92)
    assert row["judge_reasoning"] == outcome.judge_reasoning
    assert list(row["cited_evidence_fields"]) == [
        "DC9_WP!A1",
        "DC9_WP!A2",
        "DC9_WP!preparer_signoff",
    ]
    assert row["judge_status"] == "ok"
    assert row["gold_expected_verdict"] == "pass"
    assert row["fact_check_verdict"] == "pass"
    assert row["prompt_version"] == "judge_v1.0"
    assert row["model_deployment"] == "gpt-4o"
    assert row["evaluated_at"] is not None


def test_write_uncertain_with_empty_cited_evidence_fields(
    conn_factory, scoped_judge_run_id
) -> None:
    """``verdict='uncertain'`` with empty ``cited_evidence_fields``
    round-trips as an empty array (NOT NULL). The JSON ``[]`` literal
    must survive the ``from_json(..., 'array<string>')`` cast.

    This is the Decision Rule 1 exemption case — ``uncertain`` is the
    only verdict allowed to cite no evidence ("evidence is silent")."""
    outcome = _make_outcome(
        judge_run_id=scoped_judge_run_id,
        judge_verdict="uncertain",
        judge_confidence=0.3,
        judge_reasoning="evidence is silent on this point",
        cited_evidence_fields=[],
    )
    JudgeOutcomesWriter(conn_factory).write_judge_outcome(outcome)

    rows = _select_rows(conn_factory, scoped_judge_run_id)
    assert len(rows) == 1
    assert rows[0]["judge_verdict"] == "uncertain"
    assert list(rows[0]["cited_evidence_fields"]) == []


def test_two_judge_runs_same_narrative_create_two_rows(conn_factory, scoped_judge_run_id) -> None:
    """The append-only contract: two writes with the SAME
    ``narrative_run_id`` but DIFFERENT ``judge_run_id`` produce TWO
    rows, not one.

    This is the deliberate departure from ``GoldNarrativeWriter``'s
    MERGE-on-composite-key. Each sweep gets a fresh ``judge_run_id``;
    re-running a sweep against the same narratives appends new rows
    so we can do divergence-over-time analysis without losing
    history.

    The second write here uses a different scoped ``judge_run_id``
    that the fixture won't clean up automatically — we DELETE it
    explicitly at the end.
    """
    second_judge_run_id = f"test-step-06-task-04-{uuid.uuid4().hex[:12]}"
    shared_narrative_run_id = uuid.uuid4().hex.upper()

    try:
        writer = JudgeOutcomesWriter(conn_factory)
        writer.write_judge_outcome(
            _make_outcome(
                judge_run_id=scoped_judge_run_id,
                narrative_run_id=shared_narrative_run_id,
                judge_verdict="pass",
            )
        )
        writer.write_judge_outcome(
            _make_outcome(
                judge_run_id=second_judge_run_id,
                narrative_run_id=shared_narrative_run_id,
                judge_verdict="fail",
                judge_reasoning="Reviewer cell blank; claim unsupported.",
            )
        )

        rows_first = _select_rows(conn_factory, scoped_judge_run_id)
        rows_second = _select_rows(conn_factory, second_judge_run_id)
        assert len(rows_first) == 1
        assert len(rows_second) == 1
        assert rows_first[0]["judge_verdict"] == "pass"
        assert rows_second[0]["judge_verdict"] == "fail"
        assert rows_first[0]["narrative_run_id"] == shared_narrative_run_id
        assert rows_second[0]["narrative_run_id"] == shared_narrative_run_id
    finally:
        # Clean up the secondary judge_run_id; the fixture handles the first.
        with conn_factory() as conn, conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {GOLD_JUDGE_OUTCOMES_TABLE} WHERE judge_run_id = %(jrid)s",
                {"jrid": second_judge_run_id},
            )
