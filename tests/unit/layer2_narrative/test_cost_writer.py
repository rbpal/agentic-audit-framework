"""Unit tests for ``agentic_audit.layer2_narrative.cost_writer``.

Mocks the SQL connector entirely — no ``databricks-sql-connector`` dep
required at unit-test time. Mirrors ``test_gold_writer.py``: pin the
MERGE shape, the parameter mapping, and the tenacity retry behaviour.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from agentic_audit.layer2_narrative.cost_writer import (
    _MERGE_SQL,
    GOLD_COST_TELEMETRY_TABLE,
    CostTelemetryWriter,
)
from agentic_audit.models.telemetry import CostTelemetry

UTC_TS = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)


# ---------- fixtures ---------------------------------------------------


def _make_telemetry(**overrides: object) -> CostTelemetry:
    defaults: dict[str, object] = {
        "agent_run_id": "SWEEP-2026-05-09-001",
        "input_tokens": 10_000,
        "output_tokens": 4_000,
        "total_tokens": 14_000,
        "latency_ms": 120_000,
        "cost_usd": 0.065,
        "model_version": "gpt-4o",
        "started_at": UTC_TS,
        "completed_at": UTC_TS + timedelta(minutes=2),
    }
    defaults.update(overrides)
    return CostTelemetry(**defaults)  # type: ignore[arg-type]


@pytest.fixture()
def captured_calls() -> dict[str, list]:
    """Holds the (sql, params) of every cursor.execute call."""
    return {"executes": []}


@pytest.fixture()
def conn_factory_factory(captured_calls):
    """Builds a mock conn_factory that records every execute call.

    Mirrors the fixture in tests/unit/layer2_narrative/test_gold_writer.py.
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


def test_gold_cost_telemetry_table_constant_pinned() -> None:
    """The default table name is ``audit_dev.gold.cost_telemetry``.
    Changing this requires a coordinated Terraform + writer +
    integration-test update — pin it here as a regression guard."""
    assert GOLD_COST_TELEMETRY_TABLE == "audit_dev.gold.cost_telemetry"


# ---------- MERGE SQL shape --------------------------------------------


def test_merge_sql_targets_gold_cost_telemetry_table() -> None:
    assert "MERGE INTO audit_dev.gold.cost_telemetry" in _MERGE_SQL


def test_merge_sql_on_clause_uses_agent_run_id() -> None:
    """One natural key — agent_run_id. A MERGE on any other column
    would smear cohorts together."""
    assert "t.agent_run_id = s.agent_run_id" in _MERGE_SQL


def test_merge_sql_uses_parameter_markers_not_fstring_values() -> None:
    """No f-string interpolation of caller-controlled values — every
    value goes through a named parameter marker. SQL-injection guard."""
    telemetry = _make_telemetry(agent_run_id="payload'); DROP TABLE x; --")
    # The SQL string itself must NOT contain the run id.
    assert telemetry.agent_run_id not in _MERGE_SQL
    # Every value column has its parameter marker.
    for column in (
        "agent_run_id",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "latency_ms",
        "cost_usd",
        "model_version",
        "started_at",
        "completed_at",
    ):
        assert f"%({column})s" in _MERGE_SQL


def test_merge_sql_handles_both_matched_and_not_matched_branches() -> None:
    """Idempotent upsert — UPDATE on key match, INSERT otherwise."""
    assert "WHEN MATCHED THEN UPDATE SET *" in _MERGE_SQL
    assert "WHEN NOT MATCHED THEN INSERT *" in _MERGE_SQL


# ---------- _build_params ----------------------------------------------


def test_build_params_maps_every_field() -> None:
    telemetry = _make_telemetry()
    params = CostTelemetryWriter._build_params(telemetry)

    # 9 named parameters — one per table column.
    expected_keys = {
        "agent_run_id",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "latency_ms",
        "cost_usd",
        "model_version",
        "started_at",
        "completed_at",
    }
    assert set(params.keys()) == expected_keys


def test_build_params_passes_scalar_fields_unchanged() -> None:
    telemetry = _make_telemetry(
        agent_run_id="RUN-ALT",
        input_tokens=200,
        output_tokens=80,
        total_tokens=280,
        cost_usd=0.0123,
        model_version="gpt-4o-mini",
    )
    params = CostTelemetryWriter._build_params(telemetry)
    assert params["agent_run_id"] == "RUN-ALT"
    assert params["input_tokens"] == 200
    assert params["output_tokens"] == 80
    assert params["total_tokens"] == 280
    assert params["cost_usd"] == pytest.approx(0.0123)
    assert params["model_version"] == "gpt-4o-mini"


def test_build_params_preserves_none_cost_usd() -> None:
    """An unknown-deployment row carries NULL cost_usd. The writer
    must pass ``None`` straight through, not coerce to 0.0."""
    telemetry = _make_telemetry(cost_usd=None)
    params = CostTelemetryWriter._build_params(telemetry)
    assert params["cost_usd"] is None


# ---------- write_cost_telemetry — happy path --------------------------


def test_write_cost_telemetry_executes_single_merge_statement(
    conn_factory_factory, captured_calls
) -> None:
    """One MERGE per write — no separate CREATE VIEW or staging step.
    Mirrors GoldNarrativeWriter's posture."""
    telemetry = _make_telemetry()
    CostTelemetryWriter(conn_factory_factory()).write_cost_telemetry(telemetry)

    assert len(captured_calls["executes"]) == 1
    sql, params = captured_calls["executes"][0]
    assert "MERGE INTO audit_dev.gold.cost_telemetry" in sql
    assert params["agent_run_id"] == telemetry.agent_run_id
    assert params["total_tokens"] == 14_000


# ---------- write_cost_telemetry — tenacity retry ----------------------


def test_write_cost_telemetry_recovers_within_retry_limit(
    conn_factory_factory, captured_calls
) -> None:
    """Two transient failures, third succeeds — retry decorator
    swallows the first two exceptions, third call succeeds."""
    factory = conn_factory_factory(raise_first=2)
    CostTelemetryWriter(factory).write_cost_telemetry(_make_telemetry())

    assert factory.attempts["n"] == 3  # type: ignore[attr-defined]
    assert len(captured_calls["executes"]) == 1


def test_write_cost_telemetry_exhausts_retries_and_reraises(
    conn_factory_factory, captured_calls
) -> None:
    """Persistent failure raises the original exception class
    (``reraise=True`` in the decorator config)."""
    factory = conn_factory_factory(raise_first=99)

    with pytest.raises(ConnectionError, match="transient warehouse hiccup"):
        CostTelemetryWriter(factory).write_cost_telemetry(_make_telemetry())

    assert factory.attempts["n"] == 3  # type: ignore[attr-defined]
    assert captured_calls["executes"] == []
