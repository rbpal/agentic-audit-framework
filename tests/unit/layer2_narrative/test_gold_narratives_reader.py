"""Unit tests for ``agentic_audit.layer2_narrative.gold_narratives_reader``.

Mocks the Databricks SQL connector — no ``databricks-sql-connector``
dep required at unit-test time. Production wiring is exercised by
``tests/integration/test_layer2_gold_narratives_reader_e2e.py``
(env-gated; assumes ``audit_dev.gold.narratives`` is populated by a
prior ``scripts/run_layer2.py`` sweep, which seeds the 32-narrative
baseline).

Coverage matrix:

- ``iter_narratives`` issues a single SELECT against
  ``audit_dev.gold.narratives`` filtered by engagement_id + prompt_version
- Row → ``AttributeNarrative`` materialisation preserves every column
  (including ``cited_fields`` and ``fact_check_issues`` array<string>)
- Empty result raises ``GoldNarrativesReadError`` — a real sweep
  should never find an empty table; if it does, something upstream
  failed silently
- Tenacity retry recovers within limit; persistent failure exhausts

This reader is the Step 6 task_04 cycle-D dependency that gives
``scripts/run_judge_sweep.py main()`` a real narratives source. The
Step 7 supervisor will also use it.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from agentic_audit.layer2_narrative.gold_narratives_reader import (
    GOLD_NARRATIVES_TABLE,
    GoldNarrativesReader,
    GoldNarrativesReadError,
)
from agentic_audit.models.narrative import AttributeNarrative

UTC_TS = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


# ---------- fixtures ---------------------------------------------------


def _make_row(
    *,
    engagement_id: str = "alpha-pension-fund-2025",
    control_id: str = "DC-9",
    quarter: str = "Q1",
    attribute_id: str = "A",
    prompt_version: str = "v1.0",
    narrative_text: str = "Preparer signed off on the Checklist on 2026-01-10.",
    cited_fields: list[str] | None = None,
    word_count: int = 9,
    model_deployment: str = "gpt-4o",
    generation_run_id: str = "GEN_RUN_FAKE",
    generated_at: datetime = UTC_TS,
    fact_check_passed: bool = True,
    fact_check_issues: list[str] | None = None,
) -> tuple:
    """Mirror the column order in the SELECT statement; returned as a
    tuple because Databricks SQL cursor.fetchall() yields tuples."""
    if cited_fields is None:
        cited_fields = ["DC9_WP!A1"]
    if fact_check_issues is None:
        fact_check_issues = []
    source_evidence_id = f"{engagement_id}|{control_id}|{quarter}|{attribute_id}"
    return (
        engagement_id,
        control_id,
        quarter,
        attribute_id,
        prompt_version,
        source_evidence_id,
        narrative_text,
        cited_fields,  # array<string> — driver returns a Python list
        word_count,
        model_deployment,
        generation_run_id,
        generated_at,
        fact_check_passed,
        fact_check_issues,
    )


@pytest.fixture()
def captured_calls() -> dict[str, list]:
    """Holds the (sql, params) of every cursor.execute call."""
    return {"executes": []}


@pytest.fixture()
def conn_factory_factory(captured_calls):
    """Builds a mock conn_factory that records every execute call and
    returns the configured rows on fetchall.

    Mirrors test_silver_reader.py's pattern — recorder + injectable
    side-effects keep the test surface minimal.
    """

    def build(rows: list[tuple] | None = None, *, raise_first: int = 0):
        if rows is None:
            rows = []
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
            cur.fetchall.return_value = rows
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


# ---------- pinned constants -------------------------------------------


def test_gold_narratives_table_constant_points_at_dev_table() -> None:
    """Pinned so a refactor of the target table surfaces here, not
    silently as a read from the wrong source."""
    assert GOLD_NARRATIVES_TABLE == "audit_dev.gold.narratives"


# ---------- iter_narratives — single-row happy path --------------------


def test_iter_narratives_materialises_attribute_narrative_from_row(
    conn_factory_factory, captured_calls
) -> None:
    """One row → one AttributeNarrative with every field populated
    from the SELECT result.

    Pins:
    - SQL filters by engagement_id + prompt_version
    - Named parameter markers (no f-string value interpolation)
    - Every column maps to the corresponding pydantic field
    """
    row = _make_row(
        engagement_id="alpha-pension-fund-2025",
        control_id="DC-9",
        quarter="Q1",
        attribute_id="A",
        narrative_text="Preparer signed off; signature dated within Q+5.",
        cited_fields=["DC9_WP!A1", "DC9_WP!A2"],
        word_count=8,
        fact_check_passed=True,
        fact_check_issues=[],
    )
    factory = conn_factory_factory(rows=[row])
    reader = GoldNarrativesReader(factory)

    narratives = list(reader.iter_narratives("alpha-pension-fund-2025"))

    # SQL shape — spacing-tolerant by checking table name + key
    # markers rather than a full FROM clause substring.
    assert len(captured_calls["executes"]) == 1
    sql, params = captured_calls["executes"][0]
    assert GOLD_NARRATIVES_TABLE in sql
    assert "%(engagement_id)s" in sql
    assert "%(prompt_version)s" in sql
    assert params["engagement_id"] == "alpha-pension-fund-2025"
    assert params["prompt_version"] == "v1.0"  # default

    # Materialisation
    assert len(narratives) == 1
    narrative = narratives[0]
    assert isinstance(narrative, AttributeNarrative)
    assert narrative.engagement_id == "alpha-pension-fund-2025"
    assert narrative.control_id == "DC-9"
    assert narrative.attribute_id == "A"
    assert narrative.quarter == "Q1"
    assert narrative.prompt_version == "v1.0"
    assert narrative.narrative_text == "Preparer signed off; signature dated within Q+5."
    assert narrative.cited_fields == ["DC9_WP!A1", "DC9_WP!A2"]
    assert narrative.word_count == 8
    assert narrative.model_deployment == "gpt-4o"
    assert narrative.generation_run_id == "GEN_RUN_FAKE"
    assert narrative.fact_check_passed is True
    assert narrative.fact_check_issues == []


def test_iter_narratives_respects_explicit_prompt_version(
    conn_factory_factory, captured_calls
) -> None:
    """``prompt_version='v1.1'`` overrides the default; the SQL
    param carries the override."""
    factory = conn_factory_factory(rows=[_make_row(prompt_version="v1.1")])
    reader = GoldNarrativesReader(factory)

    list(reader.iter_narratives("alpha-pension-fund-2025", prompt_version="v1.1"))

    _, params = captured_calls["executes"][0]
    assert params["prompt_version"] == "v1.1"


# ---------- iter_narratives — failed fact_check round-trip -------------


def test_iter_narratives_round_trips_failed_fact_check(
    conn_factory_factory, captured_calls
) -> None:
    """``fact_check_passed=False`` rows carry ``fact_check_issues``
    populated. The judge sweep needs both signals to denormalise
    ``fact_check_verdict`` into ``gold.judge_outcomes``."""
    row = _make_row(
        fact_check_passed=False,
        fact_check_issues=["numeric not in evidence: '$2,500'"],
    )
    factory = conn_factory_factory(rows=[row])
    reader = GoldNarrativesReader(factory)

    narratives = list(reader.iter_narratives("alpha-pension-fund-2025"))

    assert narratives[0].fact_check_passed is False
    assert narratives[0].fact_check_issues == ["numeric not in evidence: '$2,500'"]


# ---------- iter_narratives — multi-row, ordering ----------------------


def test_iter_narratives_yields_all_rows_in_select_order(
    conn_factory_factory, captured_calls
) -> None:
    """N rows → N narratives, in the order the SQL returns them.

    The SQL ORDER BY pins (control_id, quarter, attribute_id) so the
    iteration order is deterministic for sweep reproducibility.
    """
    rows = [
        _make_row(control_id="DC-2", quarter="Q1", attribute_id="A"),
        _make_row(control_id="DC-2", quarter="Q1", attribute_id="C"),
        _make_row(control_id="DC-9", quarter="Q1", attribute_id="A"),
    ]
    factory = conn_factory_factory(rows=rows)
    reader = GoldNarrativesReader(factory)

    narratives = list(reader.iter_narratives("alpha-pension-fund-2025"))

    assert len(narratives) == 3
    assert [n.control_id for n in narratives] == ["DC-2", "DC-2", "DC-9"]
    assert [n.attribute_id for n in narratives] == ["A", "C", "A"]


# ---------- iter_narratives — empty result -----------------------------


def test_iter_narratives_raises_on_empty_result(conn_factory_factory) -> None:
    """No rows for the engagement → ``GoldNarrativesReadError``.

    The judge sweep should never find an empty result; if it does,
    Layer 2 (``scripts/run_layer2.py``) never wrote anything for this
    engagement, or it ran under a different prompt_version. Failing
    loud here is the breadcrumb."""
    factory = conn_factory_factory(rows=[])
    reader = GoldNarrativesReader(factory)

    with pytest.raises(GoldNarrativesReadError, match="no narratives"):
        list(reader.iter_narratives("alpha-pension-fund-2025"))


# ---------- iter_narratives — tenacity retry ---------------------------


def test_iter_narratives_recovers_within_retry_limit(conn_factory_factory) -> None:
    """Two transient failures, third succeeds — retry decorator swallows
    the first two exceptions."""
    factory = conn_factory_factory(rows=[_make_row()], raise_first=2)
    list(GoldNarrativesReader(factory).iter_narratives("alpha-pension-fund-2025"))

    assert factory.attempts["n"] == 3  # type: ignore[attr-defined]


def test_iter_narratives_exhausts_retries_and_reraises(conn_factory_factory) -> None:
    """Retry decorator caps at 3 attempts; persistent failure raises
    the original exception class (``reraise=True``)."""
    factory = conn_factory_factory(rows=[_make_row()], raise_first=99)

    with pytest.raises(ConnectionError, match="transient warehouse hiccup"):
        list(GoldNarrativesReader(factory).iter_narratives("alpha-pension-fund-2025"))

    assert factory.attempts["n"] == 3  # type: ignore[attr-defined]
