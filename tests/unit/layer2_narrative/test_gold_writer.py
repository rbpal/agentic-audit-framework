"""Unit tests for ``agentic_audit.layer2_narrative.gold_writer``.

Mocks the SQL connector entirely — no ``databricks-sql-connector`` dep
required at unit-test time. Production wiring is exercised by
``tests/integration/test_layer2_gold_writer_e2e.py``
(``@pytest.mark.slow``, env-gated).

Coverage matrix:

- ``_build_params`` maps every ``AttributeNarrative`` field to a named
  parameter; arrays serialise as JSON for ``from_json`` cast.
- The MERGE SQL contains all 5 composite-key columns in the ``ON``
  clause (no silent partial-key MERGE).
- The MERGE SQL targets ``audit_dev.gold.narratives`` and uses
  parameter markers (no f-string interpolation of values).
- Tenacity retry behaviour: transient failure recovers within
  retries; persistent failure exhausts and re-raises.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from agentic_audit.layer2_narrative.gold_writer import (
    _MERGE_SQL,
    GOLD_NARRATIVES_TABLE,
    GoldNarrativeWriter,
)
from agentic_audit.models.narrative import AttributeNarrative

UTC_TS = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)


# ---------- fixtures ---------------------------------------------------


def _make_narrative(
    *,
    engagement_id: str = "alpha-pension-fund-2025",
    control_id: str = "DC-9",
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
        control_id=control_id,  # type: ignore[arg-type]
        attribute_id=attribute_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        source_evidence_id=f"{engagement_id}|{control_id}|{quarter}|{attribute_id}",
        narrative_text=narrative_text,
        cited_fields=cited_fields,
        word_count=len(narrative_text.split()),
        prompt_version=prompt_version,
        model_deployment="gpt-4o",
        generation_run_id="01J0F7M5XQXM2QYAY8X8X8X8X8",
        generated_at=UTC_TS,
        fact_check_passed=fact_check_passed,
        fact_check_issues=fact_check_issues,
    )


@pytest.fixture()
def captured_calls() -> dict[str, list]:
    """Holds the (sql, params) of every cursor.execute call."""
    return {"executes": []}


@pytest.fixture()
def conn_factory_factory(captured_calls):
    """Builds a mock conn_factory that records every execute call.

    Mirrors the pattern in tests/unit/layer1_extract/test_silver_writer.py.
    """

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


# ---------- pinned constants -------------------------------------------


def test_gold_table_constant_pinned() -> None:
    """The default table name is ``audit_dev.gold.narratives``. Changing
    this requires a coordinated Terraform + writer + integration-test
    update — pinning it here is the regression guard."""
    assert GOLD_NARRATIVES_TABLE == "audit_dev.gold.narratives"


# ---------- MERGE SQL shape --------------------------------------------


def test_merge_sql_targets_gold_narratives_table() -> None:
    assert "MERGE INTO audit_dev.gold.narratives" in _MERGE_SQL


def test_merge_sql_on_clause_includes_all_five_key_columns() -> None:
    """The composite primary key has 5 columns. A partial-key MERGE
    would silently overwrite rows from a different attribute or prompt
    version. Pin every column."""
    for key_col in (
        "engagement_id",
        "control_id",
        "quarter",
        "attribute_id",
        "prompt_version",
    ):
        assert f"t.{key_col}" in _MERGE_SQL
        assert f"s.{key_col}" in _MERGE_SQL


def test_merge_sql_uses_parameter_markers_not_fstring_values() -> None:
    """No f-string interpolation of caller-controlled values — every
    value goes through a named parameter marker. Regression guard for
    the SQL-injection surface."""
    narrative = _make_narrative(narrative_text="payload with '; DROP TABLE narratives; --")
    # The SQL string itself must NOT contain the narrative text.
    assert narrative.narrative_text not in _MERGE_SQL
    # And the parameter marker must be there.
    assert "%(narrative_text)s" in _MERGE_SQL


def test_merge_sql_casts_arrays_via_from_json() -> None:
    """Variable-length arrays are JSON-serialised by the writer and
    cast in-statement via ``from_json(..., 'array<string>')`` — the
    cleanest portable shape for parametrised arrays."""
    assert "from_json(%(cited_fields_json)s, 'array<string>')" in _MERGE_SQL
    assert "from_json(%(fact_check_issues_json)s, 'array<string>')" in _MERGE_SQL


def test_merge_sql_handles_both_matched_and_not_matched_branches() -> None:
    """Idempotent upsert — UPDATE on key match, INSERT otherwise."""
    assert "WHEN MATCHED THEN UPDATE SET *" in _MERGE_SQL
    assert "WHEN NOT MATCHED THEN INSERT *" in _MERGE_SQL


# ---------- _build_params ----------------------------------------------


def test_build_params_maps_every_field() -> None:
    narrative = _make_narrative()
    params = GoldNarrativeWriter._build_params(narrative)

    # All 14 named parameters present (12 scalar + 2 JSON-serialised arrays)
    expected_keys = {
        "engagement_id",
        "control_id",
        "quarter",
        "attribute_id",
        "prompt_version",
        "source_evidence_id",
        "narrative_text",
        "cited_fields_json",
        "word_count",
        "model_deployment",
        "generation_run_id",
        "generated_at",
        "fact_check_passed",
        "fact_check_issues_json",
    }
    assert set(params.keys()) == expected_keys


def test_build_params_serialises_cited_fields_as_json() -> None:
    narrative = _make_narrative(cited_fields=["DC9_WP!A1", "DC9_WP!A2"])
    params = GoldNarrativeWriter._build_params(narrative)
    assert json.loads(params["cited_fields_json"]) == [
        "DC9_WP!A1",
        "DC9_WP!A2",
    ]


def test_build_params_serialises_fact_check_issues_as_json() -> None:
    narrative = _make_narrative(
        fact_check_passed=False,
        fact_check_issues=["numeric not in evidence: '$2,500'"],
    )
    params = GoldNarrativeWriter._build_params(narrative)
    assert json.loads(params["fact_check_issues_json"]) == ["numeric not in evidence: '$2,500'"]


def test_build_params_handles_empty_arrays() -> None:
    """Empty cited_fields and empty fact_check_issues serialise as
    valid JSON arrays, not nulls — ``from_json`` would error on
    ``None``."""
    narrative = _make_narrative(cited_fields=[], fact_check_issues=[])
    params = GoldNarrativeWriter._build_params(narrative)
    assert params["cited_fields_json"] == "[]"
    assert params["fact_check_issues_json"] == "[]"


def test_build_params_passes_scalar_fields_unchanged() -> None:
    narrative = _make_narrative(
        engagement_id="bravo-2026",
        control_id="DC-2",
        attribute_id="C",
        quarter="Q3",
        prompt_version="v1.1",
    )
    params = GoldNarrativeWriter._build_params(narrative)
    assert params["engagement_id"] == "bravo-2026"
    assert params["control_id"] == "DC-2"
    assert params["attribute_id"] == "C"
    assert params["quarter"] == "Q3"
    assert params["prompt_version"] == "v1.1"
    assert params["word_count"] == narrative.word_count
    assert params["fact_check_passed"] is True


# ---------- write_narrative — happy path -------------------------------


def test_write_narrative_executes_single_merge_statement(
    conn_factory_factory, captured_calls
) -> None:
    """One MERGE statement per write_narrative call — no separate
    CREATE VIEW or staging step. Mirrors the SilverWriter posture."""
    narrative = _make_narrative()
    GoldNarrativeWriter(conn_factory_factory()).write_narrative(narrative)

    assert len(captured_calls["executes"]) == 1
    sql, params = captured_calls["executes"][0]
    assert "MERGE INTO audit_dev.gold.narratives" in sql
    assert params["narrative_text"] == narrative.narrative_text
    assert params["prompt_version"] == "v1.0"


def test_write_narrative_passes_jsonified_arrays_to_cursor(
    conn_factory_factory, captured_calls
) -> None:
    narrative = _make_narrative(
        cited_fields=["DC9_WP!A1", "DC9_WP!B2"],
        fact_check_passed=False,
        fact_check_issues=["numeric not in evidence: '$2,500'"],
    )
    GoldNarrativeWriter(conn_factory_factory()).write_narrative(narrative)

    _, params = captured_calls["executes"][0]
    assert json.loads(params["cited_fields_json"]) == [
        "DC9_WP!A1",
        "DC9_WP!B2",
    ]
    assert json.loads(params["fact_check_issues_json"]) == ["numeric not in evidence: '$2,500'"]
    assert params["fact_check_passed"] is False


# ---------- write_narrative — tenacity retry ---------------------------


def test_write_narrative_recovers_within_retry_limit(conn_factory_factory, captured_calls) -> None:
    """Two transient failures, third succeeds — retry decorator
    swallows the first two exceptions, third call succeeds."""
    factory = conn_factory_factory(raise_first=2)
    GoldNarrativeWriter(factory).write_narrative(_make_narrative())

    # Three connect attempts, one successful execute
    assert factory.attempts["n"] == 3  # type: ignore[attr-defined]
    assert len(captured_calls["executes"]) == 1


def test_write_narrative_exhausts_retries_and_reraises(
    conn_factory_factory, captured_calls
) -> None:
    """Retry decorator caps at 3 attempts; persistent failure raises
    the original exception class (``reraise=True`` in the decorator
    config)."""
    factory = conn_factory_factory(raise_first=99)  # always fails

    with pytest.raises(ConnectionError, match="transient warehouse hiccup"):
        GoldNarrativeWriter(factory).write_narrative(_make_narrative())

    assert factory.attempts["n"] == 3  # type: ignore[attr-defined]
    assert captured_calls["executes"] == []
