"""Unit tests for ``agentic_audit.layer2_narrative.judge_outcomes_writer``.

Mocks the Databricks SQL connector — no ``databricks-sql-connector``
dep required at unit-test time. Production wiring is exercised by
``tests/integration/test_layer2_judge_outcomes_writer_e2e.py``
(env-gated; needs the real ``audit_dev.gold.judge_outcomes`` provisioned
in dev, which step_06_task_04's Terraform PR landed).

Coverage matrix:

- ``_build_params`` maps every ``JudgeOutcomeRow`` field to a named
  parameter; ``cited_evidence_fields`` serialises as JSON for the
  ``from_json(..., 'array<string>')`` cast.
- The INSERT SQL targets ``audit_dev.gold.judge_outcomes`` and uses
  parameter markers (no f-string interpolation of values).
- Tenacity retry behaviour: transient failure recovers within
  retries; persistent failure exhausts and re-raises.

Design departs from ``GoldNarrativeWriter`` in one way: this writer
is **append-only** (Step 6 task_04 Q7 decision). Each sweep gets a
fresh ``judge_run_id`` and inserts new rows; we never MERGE/UPDATE.
Re-running a sweep adds rows; it does not overwrite. This is the
Step 5 task_05 lesson applied — divergence-over-time is the asset.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from agentic_audit.layer2_narrative.judge_outcomes_writer import (
    GOLD_JUDGE_OUTCOMES_TABLE,
    JudgeOutcomesWriter,
)
from agentic_audit.models.judge import JudgeOutcomeRow

UTC_TS = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


# ---------- fixtures ---------------------------------------------------


def _make_outcome(
    *,
    judge_run_id: str = "JUDGE_RUN_TEST",
    narrative_run_id: str = "GEN_RUN_FAKE",
    engagement_id: str = "alpha-pension-fund-2025",
    control_id: str = "DC-9",
    attribute_id: str = "A",
    quarter: str = "Q1",
    judge_verdict: str = "pass",
    judge_confidence: float = 0.9,
    judge_reasoning: str = "Evidence supports the claim.",
    cited_evidence_fields: list[str] | None = None,
    judge_status: str = "ok",
    gold_expected_verdict: str = "pass",
    fact_check_verdict: str = "pass",
    prompt_version: str = "judge_v1.0",
    model_deployment: str = "gpt-4o",
    evaluated_at: datetime = UTC_TS,
) -> JudgeOutcomeRow:
    if cited_evidence_fields is None:
        cited_evidence_fields = ["DC9_WP!A1"]
    return JudgeOutcomeRow(
        judge_run_id=judge_run_id,
        narrative_run_id=narrative_run_id,
        engagement_id=engagement_id,
        control_id=control_id,  # type: ignore[arg-type]
        attribute_id=attribute_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        judge_verdict=judge_verdict,  # type: ignore[arg-type]
        judge_confidence=judge_confidence,
        judge_reasoning=judge_reasoning,
        cited_evidence_fields=cited_evidence_fields,
        judge_status=judge_status,  # type: ignore[arg-type]
        gold_expected_verdict=gold_expected_verdict,
        fact_check_verdict=fact_check_verdict,  # type: ignore[arg-type]
        prompt_version=prompt_version,
        model_deployment=model_deployment,
        evaluated_at=evaluated_at,
    )


@pytest.fixture()
def captured_calls() -> dict[str, list]:
    """Holds the (sql, params) of every cursor.execute call."""
    return {"executes": []}


@pytest.fixture()
def conn_factory_factory(captured_calls):
    """Builds a mock conn_factory that records every execute call.

    Mirrors the pattern in tests/unit/layer2_narrative/test_gold_writer.py
    so the two writers' tests are read side-by-side.
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


def test_gold_judge_outcomes_table_constant_points_at_dev_table() -> None:
    """Pinned so a refactor of the target table surfaces here, not
    silently as a write to the wrong destination."""
    assert GOLD_JUDGE_OUTCOMES_TABLE == "audit_dev.gold.judge_outcomes"


# ---------- happy path: one INSERT, all 16 named params ----------------


def test_writer_inserts_one_outcome_with_named_params(conn_factory_factory, captured_calls) -> None:
    """One ``JudgeOutcomeRow`` → one ``cursor.execute`` call with INSERT
    SQL targeting ``audit_dev.gold.judge_outcomes`` and 16 named
    parameters carrying the row's data.

    Pins:
    - INSERT (not MERGE) — append-only contract.
    - Named parameter markers (``%(name)s``) for every value — no
      f-string interpolation of values into SQL.
    - ``cited_evidence_fields`` serialised as JSON for the in-statement
      ``from_json(..., 'array<string>')`` cast.
    """
    factory = conn_factory_factory()
    writer = JudgeOutcomesWriter(factory)
    outcome = _make_outcome()

    writer.write_judge_outcome(outcome)

    assert len(captured_calls["executes"]) == 1
    sql, params = captured_calls["executes"][0]

    # SQL shape
    assert f"INSERT INTO {GOLD_JUDGE_OUTCOMES_TABLE}" in sql
    assert "%(judge_run_id)s" in sql
    assert "from_json(%(cited_evidence_fields_json)s, 'array<string>')" in sql

    # Scalar parameter mapping — 15 fields
    assert params["judge_run_id"] == outcome.judge_run_id
    assert params["narrative_run_id"] == outcome.narrative_run_id
    assert params["engagement_id"] == outcome.engagement_id
    assert params["control_id"] == outcome.control_id
    assert params["attribute_id"] == outcome.attribute_id
    assert params["quarter"] == outcome.quarter
    assert params["judge_verdict"] == outcome.judge_verdict
    assert params["judge_confidence"] == outcome.judge_confidence
    assert params["judge_reasoning"] == outcome.judge_reasoning
    assert params["judge_status"] == outcome.judge_status
    assert params["gold_expected_verdict"] == outcome.gold_expected_verdict
    assert params["fact_check_verdict"] == outcome.fact_check_verdict
    assert params["prompt_version"] == outcome.prompt_version
    assert params["model_deployment"] == outcome.model_deployment
    assert params["evaluated_at"] == outcome.evaluated_at

    # Array field — JSON-encoded for the from_json cast
    assert params["cited_evidence_fields_json"] == json.dumps(outcome.cited_evidence_fields)


def test_writer_handles_empty_cited_evidence_fields_for_uncertain(
    conn_factory_factory, captured_calls
) -> None:
    """A judge ``uncertain`` verdict is allowed to cite no evidence
    (Decision Rule 1 exemption in ``JudgeResponse``). The writer must
    round-trip an empty array via the JSON cast — not crash on the
    empty list, not skip the field.
    """
    outcome = _make_outcome(
        judge_verdict="uncertain",
        judge_confidence=0.3,
        judge_reasoning="evidence is silent on this point",
        cited_evidence_fields=[],
    )
    JudgeOutcomesWriter(conn_factory_factory()).write_judge_outcome(outcome)

    _, params = captured_calls["executes"][0]
    assert params["cited_evidence_fields_json"] == "[]"
    assert params["judge_verdict"] == "uncertain"


# ---------- write_judge_outcome — tenacity retry -----------------------


def test_write_judge_outcome_recovers_within_retry_limit(
    conn_factory_factory, captured_calls
) -> None:
    """Two transient failures, third succeeds — retry decorator
    swallows the first two exceptions, third call succeeds. Mirrors
    GoldNarrativeWriter's posture; sweep continues without surfacing
    the transient warehouse hiccup to the operator."""
    factory = conn_factory_factory(raise_first=2)
    JudgeOutcomesWriter(factory).write_judge_outcome(_make_outcome())

    assert factory.attempts["n"] == 3  # type: ignore[attr-defined]
    assert len(captured_calls["executes"]) == 1


def test_write_judge_outcome_exhausts_retries_and_reraises(
    conn_factory_factory, captured_calls
) -> None:
    """Retry decorator caps at 3 attempts; persistent failure raises
    the original exception class (``reraise=True``). run_sweep's
    per-row try/except (cycle A) absorbs this so the sweep continues,
    but the writer itself must not silently swallow."""
    factory = conn_factory_factory(raise_first=99)

    with pytest.raises(ConnectionError, match="transient warehouse hiccup"):
        JudgeOutcomesWriter(factory).write_judge_outcome(_make_outcome())

    assert factory.attempts["n"] == 3  # type: ignore[attr-defined]
    assert captured_calls["executes"] == []
