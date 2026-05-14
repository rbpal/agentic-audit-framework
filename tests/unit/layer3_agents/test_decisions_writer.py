"""Unit tests for ``Layer3DecisionsWriter`` (Step 7 task_08).

Mirrors the GoldNarrativeWriter test shape — mock conn_factory,
assert the SQL parameters carry the model fields verbatim,
verify tenacity retries on transient failure, and check that
``citations`` (array<string>) is JSON-serialised for the in-statement
``from_json`` cast.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentic_audit.layer3_agents.decisions_writer import (
    GOLD_LAYER3_DECISIONS_TABLE,
    Layer3DecisionsWriter,
)
from agentic_audit.models.layer3_decision import Layer3Decision

UTC_TS = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


def _ok_decision(**overrides: Any) -> Layer3Decision:
    base = {
        "agent_run_id": "sweep-1",
        "investigation_run_id": "inv-1",
        "engagement_id": "eng-1",
        "control_id": "DC-9",
        "attribute_id": "D",
        "quarter": "Q3",
        "exception_type": "billing_rate_change",
        "final_verdict": "pass",
        "final_confidence": 0.92,
        "iterations_used": 3,
        "status": "concluded",
        "narrative_text": "Rate moved from 28.5 to 30.0 per the amendment.",
        "citations": ["sheet1!A12", "amendment.pdf!p2"],
        "recommendation": "ACCEPT",
        "tool_trace": "[]",
        "judge_verdict": "pass",
        "judge_confidence": 0.91,
        "prompt_version": "extraction_v1.0|validation_v1.0|narrative_v1.1",
        "model_deployment": "gpt-4o",
        "decided_at": UTC_TS,
    }
    base.update(overrides)
    return Layer3Decision(**base)  # type: ignore[arg-type]


def _build_writer_with_mock_conn() -> tuple[Layer3DecisionsWriter, MagicMock]:
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn_ctx = MagicMock()
    conn_ctx.__enter__.return_value = conn
    factory = MagicMock(return_value=conn_ctx)
    return Layer3DecisionsWriter(conn_factory=factory), cursor


# ── Schema validation ────────────────────────────────────────────────


def test_layer3_decision_validates_recommendation_enum() -> None:
    """Recommendation must be one of {ACCEPT, ESCALATE} per the
    state.Recommendation Literal. Catches a downstream consumer
    sending a free-form string."""
    with pytest.raises(Exception, match="recommendation"):
        _ok_decision(recommendation="MAYBE")


def test_layer3_decision_validates_final_verdict_enum() -> None:
    with pytest.raises(Exception, match="final_verdict"):
        _ok_decision(final_verdict="probably")


def test_layer3_decision_validates_status_enum() -> None:
    with pytest.raises(Exception, match="status"):
        _ok_decision(status="investigating")  # not a terminal state


def test_layer3_decision_rejects_extra_fields() -> None:
    """``extra='forbid'`` keeps the model schema-locked. A typo'd
    field name surfaces at construction, not silently drops in the
    INSERT."""
    with pytest.raises(Exception, match="extra"):
        _ok_decision(unknown_field="oops")


def test_layer3_decision_clamps_final_confidence_in_unit_interval() -> None:
    with pytest.raises(Exception, match="confidence"):
        _ok_decision(final_confidence=1.5)
    with pytest.raises(Exception, match="confidence"):
        _ok_decision(final_confidence=-0.1)


def test_layer3_decision_allows_null_judge_fields() -> None:
    """Judge fields are nullable on escalate paths where the judge
    wasn't consulted. Pin the contract."""
    decision = _ok_decision(judge_verdict=None, judge_confidence=None)
    assert decision.judge_verdict is None
    assert decision.judge_confidence is None


# ── INSERT SQL emission ──────────────────────────────────────────────


def test_write_decision_executes_insert_with_target_table() -> None:
    writer, cursor = _build_writer_with_mock_conn()
    writer.write_decision(_ok_decision())
    sql, params = cursor.execute.call_args[0]
    assert GOLD_LAYER3_DECISIONS_TABLE in sql
    assert "INSERT INTO" in sql


def test_write_decision_passes_named_params_for_every_column() -> None:
    """Every Layer3Decision field must show up in the INSERT params
    keyed by its column name. A missed binding silently drops a
    column to NULL on the warehouse — surface that here."""
    writer, cursor = _build_writer_with_mock_conn()
    decision = _ok_decision()
    writer.write_decision(decision)
    _sql, params = cursor.execute.call_args[0]
    for key in (
        "agent_run_id",
        "investigation_run_id",
        "engagement_id",
        "control_id",
        "attribute_id",
        "quarter",
        "exception_type",
        "final_verdict",
        "final_confidence",
        "iterations_used",
        "status",
        "narrative_text",
        "citations_json",
        "recommendation",
        "tool_trace",
        "judge_verdict",
        "judge_confidence",
        "prompt_version",
        "model_deployment",
        "decided_at",
    ):
        assert key in params, f"INSERT params missing {key}"


def test_write_decision_serialises_citations_array_to_json() -> None:
    """``citations`` is stored as ``array<string>`` on Databricks; the
    parameter binding passes a JSON string + the SQL casts via
    ``from_json(..., 'array<string>')``. Pin both sides."""
    writer, cursor = _build_writer_with_mock_conn()
    citations = ["sheet1!A12", "amendment.pdf!p2"]
    writer.write_decision(_ok_decision(citations=citations))
    sql, params = cursor.execute.call_args[0]
    assert "from_json(%(citations_json)s, 'array<string>')" in sql
    assert json.loads(params["citations_json"]) == citations


def test_write_decision_passes_judge_nulls_through() -> None:
    """Escalate paths null out judge_verdict + judge_confidence. The
    INSERT must transmit them as None, not coerce to 'None' strings."""
    writer, cursor = _build_writer_with_mock_conn()
    writer.write_decision(_ok_decision(judge_verdict=None, judge_confidence=None))
    _sql, params = cursor.execute.call_args[0]
    assert params["judge_verdict"] is None
    assert params["judge_confidence"] is None


# ── Tenacity retry posture ───────────────────────────────────────────


def test_write_decision_retries_on_transient_failure_then_succeeds() -> None:
    """Mirror the GoldNarrativeWriter contract: tenacity retries on
    any exception, capped at 3 attempts. First attempt fails, second
    succeeds → write completes; the cursor's execute call count is 2."""
    cursor = MagicMock()
    cursor.execute.side_effect = [Exception("transient"), None]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn_ctx = MagicMock()
    conn_ctx.__enter__.return_value = conn
    factory = MagicMock(return_value=conn_ctx)
    writer = Layer3DecisionsWriter(conn_factory=factory)

    writer.write_decision(_ok_decision())

    assert cursor.execute.call_count == 2


def test_write_decision_reraises_after_three_failures() -> None:
    """3 consecutive failures → original exception bubbles up
    (tenacity ``reraise=True``)."""
    cursor = MagicMock()
    cursor.execute.side_effect = [
        Exception("fail-1"),
        Exception("fail-2"),
        Exception("fail-3"),
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn_ctx = MagicMock()
    conn_ctx.__enter__.return_value = conn
    factory = MagicMock(return_value=conn_ctx)
    writer = Layer3DecisionsWriter(conn_factory=factory)

    with pytest.raises(Exception, match="fail-3"):
        writer.write_decision(_ok_decision())
