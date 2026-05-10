"""Gold-layer writer for Layer 2 judge verdicts (Step 6 task_04).

Writes one ``JudgeOutcomeRow`` to ``audit_dev.gold.judge_outcomes`` per
call. **Append-only** — each sweep gets a fresh ``judge_run_id``;
re-running a sweep adds rows, it does not overwrite. Divergence-
over-time is the asset (Step 6 task_04 Q7).

Design follows the Layer 2 ``GoldNarrativeWriter`` pattern with one
deliberate departure:

- ``conn_factory`` is dependency-injected so tests mock the connector
  while production wires it to ``databricks.sql.connect``.
- INSERT (not MERGE). The append-only contract means re-running a
  sweep is fine — new ``judge_run_id`` values land as parallel rows
  for divergence queries to inspect.
- ``cited_evidence_fields`` (``array<string>``) is passed as a JSON
  string and cast in-statement via ``from_json(..., 'array<string>')``.
  Same shape rationale as ``GoldNarrativeWriter``.
- Tenacity retries the whole ``write_judge_outcome`` on any exception,
  capped at 3 attempts with ``reraise=True`` so callers see the
  original exception class.

See ``privateDocs/step_06_eval_harness.md`` task_04 for the table
schema, append-only contract, and operational defaults.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from tenacity import retry, stop_after_attempt, wait_exponential

from agentic_audit.models.judge import JudgeOutcomeRow
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager


GOLD_JUDGE_OUTCOMES_TABLE = "audit_dev.gold.judge_outcomes"


# Single-statement INSERT. Named parameter markers for every value so
# the binding stays simple; the cited_evidence_fields array is cast
# via from_json so the parameter is a JSON string.
_INSERT_SQL = f"""
INSERT INTO {GOLD_JUDGE_OUTCOMES_TABLE} (
    judge_run_id,
    narrative_run_id,
    engagement_id,
    control_id,
    attribute_id,
    quarter,
    judge_verdict,
    judge_confidence,
    judge_reasoning,
    cited_evidence_fields,
    judge_status,
    gold_expected_verdict,
    fact_check_verdict,
    prompt_version,
    model_deployment,
    evaluated_at
)
VALUES (
    %(judge_run_id)s,
    %(narrative_run_id)s,
    %(engagement_id)s,
    %(control_id)s,
    %(attribute_id)s,
    %(quarter)s,
    %(judge_verdict)s,
    %(judge_confidence)s,
    %(judge_reasoning)s,
    from_json(%(cited_evidence_fields_json)s, 'array<string>'),
    %(judge_status)s,
    %(gold_expected_verdict)s,
    %(fact_check_verdict)s,
    %(prompt_version)s,
    %(model_deployment)s,
    %(evaluated_at)s
)
"""


class JudgeOutcomesWriter:
    """Writes ``JudgeOutcomeRow`` records to ``audit_dev.gold.judge_outcomes``.

    Pass a zero-arg ``conn_factory`` callable that returns a
    context-managed Databricks SQL connection. Production wires it to
    ``databricks.sql.connect``; tests wire it to a mock.

    Append-only: there is no UPDATE path. Each ``judge_run_id`` lands
    as a fresh row; divergence-over-time is the table's purpose.
    """

    def __init__(
        self,
        conn_factory: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        self._conn_factory = conn_factory

    @traced_function("layer2.judge_outcomes_writer.write_judge_outcome")
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=10),
        reraise=True,
    )
    def write_judge_outcome(self, outcome: JudgeOutcomeRow) -> None:
        """INSERT one judge outcome into ``audit_dev.gold.judge_outcomes``."""
        params = self._build_params(outcome)
        with self._conn_factory() as conn, conn.cursor() as cur:
            cur.execute(_INSERT_SQL, params)

    @staticmethod
    def _build_params(outcome: JudgeOutcomeRow) -> dict[str, Any]:
        """Map a ``JudgeOutcomeRow`` to the INSERT statement's named
        parameters. ``cited_evidence_fields`` is serialised to a JSON
        string for the in-statement ``from_json(..., 'array<string>')``
        cast.
        """
        return {
            "judge_run_id": outcome.judge_run_id,
            "narrative_run_id": outcome.narrative_run_id,
            "engagement_id": outcome.engagement_id,
            "control_id": outcome.control_id,
            "attribute_id": outcome.attribute_id,
            "quarter": outcome.quarter,
            "judge_verdict": outcome.judge_verdict,
            "judge_confidence": outcome.judge_confidence,
            "judge_reasoning": outcome.judge_reasoning,
            "cited_evidence_fields_json": json.dumps(outcome.cited_evidence_fields),
            "judge_status": outcome.judge_status,
            "gold_expected_verdict": outcome.gold_expected_verdict,
            "fact_check_verdict": outcome.fact_check_verdict,
            "prompt_version": outcome.prompt_version,
            "model_deployment": outcome.model_deployment,
            "evaluated_at": outcome.evaluated_at,
        }


__all__ = [
    "GOLD_JUDGE_OUTCOMES_TABLE",
    "JudgeOutcomesWriter",
]
