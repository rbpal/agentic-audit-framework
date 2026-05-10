"""End-to-end integration test for GoldNarrativesReader (step_06_task_04 cycle D).

Marked ``@pytest.mark.slow`` and gated on ``DATABRICKS_SQL_WAREHOUSE_ID``
env var. CI's default unit-test pass skips this. Run on demand with::

    DATABRICKS_HOST=...                                              \\
    DATABRICKS_TOKEN=...                                             \\
    DATABRICKS_SQL_WAREHOUSE_ID=...                                  \\
    poetry run pytest -m slow tests/integration/test_layer2_gold_narratives_reader_e2e.py -v

Assumes ``audit_dev.gold.narratives`` exists and is populated. The
test scopes by ``engagement_id`` prefix and DELETEs its rows on
teardown — no pollution of the live 32-narrative dev baseline that
the Step 6 judge sweep reads from.

What this verifies:

1. **Round-trip via GoldNarrativeWriter -> GoldNarrativesReader**:
   write one ``AttributeNarrative`` through the existing writer,
   read it back via the new reader, assert every field matches.
2. **Empty result raises GoldNarrativesReadError**: querying for an
   engagement that doesn't exist raises the typed error so callers
   can distinguish "Layer 2 hasn't run yet" from a transient warehouse
   failure.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from agentic_audit.layer2_narrative.gold_narratives_reader import (
    GOLD_NARRATIVES_TABLE,
    GoldNarrativesReader,
    GoldNarrativesReadError,
)
from agentic_audit.layer2_narrative.gold_writer import GoldNarrativeWriter
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
    """Yield a test-scoped engagement_id and DELETE its rows on teardown."""
    engagement = f"test-step-06-task-04-reader-{uuid.uuid4().hex[:12]}"
    yield engagement
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
    narrative_text: str = "Preparer signature dated within Q+5; tied to W/P A1.",
    cited_fields: list[str] | None = None,
    fact_check_passed: bool = True,
    fact_check_issues: list[str] | None = None,
) -> AttributeNarrative:
    if cited_fields is None:
        cited_fields = ["DC9_WP!A1", "DC9_WP!A2"]
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
        prompt_version="v1.0",
        model_deployment="gpt-4o",
        generation_run_id=uuid.uuid4().hex.upper(),
        generated_at=datetime.now(UTC),
        fact_check_passed=fact_check_passed,
        fact_check_issues=fact_check_issues,
    )


def test_round_trip_via_writer_then_reader(conn_factory, scoped_engagement_id) -> None:
    """Write one narrative via GoldNarrativeWriter, read it back via
    GoldNarrativesReader. Every field matches what was written.

    The reader's job is to give the judge sweep a faithful
    AttributeNarrative reconstruction — same shape Layer 2 generated,
    no synthesised fields, no lossy parsing.
    """
    original = _make_narrative(engagement_id=scoped_engagement_id)
    GoldNarrativeWriter(conn_factory).write_narrative(original)

    reader = GoldNarrativesReader(conn_factory)
    narratives = list(reader.iter_narratives(scoped_engagement_id))

    assert len(narratives) == 1
    rt = narratives[0]
    assert rt.engagement_id == original.engagement_id
    assert rt.control_id == original.control_id
    assert rt.attribute_id == original.attribute_id
    assert rt.quarter == original.quarter
    assert rt.prompt_version == original.prompt_version
    assert rt.source_evidence_id == original.source_evidence_id
    assert rt.narrative_text == original.narrative_text
    assert rt.cited_fields == original.cited_fields
    assert rt.word_count == original.word_count
    assert rt.model_deployment == original.model_deployment
    assert rt.generation_run_id == original.generation_run_id
    assert rt.fact_check_passed is True
    assert rt.fact_check_issues == []


def test_iter_narratives_raises_when_engagement_missing(conn_factory) -> None:
    """Querying for an engagement_id that doesn't exist raises
    GoldNarrativesReadError — the breadcrumb that says "Layer 2
    hasn't been run for this engagement (or under this prompt_version)."
    """
    reader = GoldNarrativesReader(conn_factory)
    nonexistent = f"never-existed-{uuid.uuid4().hex[:12]}"

    with pytest.raises(GoldNarrativesReadError, match="no narratives"):
        list(reader.iter_narratives(nonexistent))
