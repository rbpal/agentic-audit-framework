"""Gold-layer writer for Layer-3 supervisor decisions (Step 7 task_08).

Writes one ``Layer3Decision`` row to ``audit_dev.gold.layer3_decisions``
per call. **Append-only** — keyed by per-investigation ULID
(``investigation_run_id``), so re-running the supervisor on the same
scope produces a new row, NOT an overwrite. This is intentional: the
audit trail must preserve every prompt-version + model-deployment
cohort the supervisor ever ran against.

Design follows ``GoldNarrativeWriter`` + ``CostTelemetryWriter``:

- ``conn_factory`` is dependency-injected so tests mock the connector
  while production wires it to ``databricks.sql.connect``.
- The INSERT statement uses parameter markers (``%(name)s``) for every
  value — Databricks SQL allows them in DML.
- ``citations`` (``array<string>``) is passed as a JSON string and
  cast in-statement via ``from_json(..., 'array<string>')`` — same
  pattern Layer 2's narrative writer uses for ``cited_fields``.
- Tenacity retries the whole ``write_decision`` on any exception,
  capped at 3 attempts with ``reraise=True`` so callers see the
  original exception class.
- ``@traced_function`` emits span_start / span_end records on the
  ``agentic_audit.trace`` logger.

See ``privateDocs/step_07_layer3_multiagent.md`` ``step_07_task_00``
for the table-schema design rationale (Option C: slim verdict + JSON
trace pointer).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from tenacity import retry, stop_after_attempt, wait_exponential

from agentic_audit.models.layer3_decision import Layer3Decision
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager


GOLD_LAYER3_DECISIONS_TABLE = "audit_dev.gold.layer3_decisions"


# Single-statement INSERT (NOT MERGE). The supervisor mints a fresh
# ULID-style ``investigation_run_id`` on every invocation; collisions
# are not expected, so we don't pay for MERGE's match-and-update
# round-trip. Rejected MERGE alternative documented in
# privateDocs/step_07_layer3_multiagent.md task_08.
#
# ``citations`` (array<string>) is passed as a JSON string and cast
# in-statement — same pattern as ``gold.narratives.cited_fields``.
_INSERT_SQL = f"""
INSERT INTO {GOLD_LAYER3_DECISIONS_TABLE} (
    agent_run_id,
    investigation_run_id,
    engagement_id,
    control_id,
    attribute_id,
    quarter,
    exception_type,
    final_verdict,
    final_confidence,
    iterations_used,
    status,
    narrative_text,
    citations,
    recommendation,
    tool_trace,
    judge_verdict,
    judge_confidence,
    prompt_version,
    model_deployment,
    decided_at
)
SELECT
    %(agent_run_id)s,
    %(investigation_run_id)s,
    %(engagement_id)s,
    %(control_id)s,
    %(attribute_id)s,
    %(quarter)s,
    %(exception_type)s,
    %(final_verdict)s,
    %(final_confidence)s,
    %(iterations_used)s,
    %(status)s,
    %(narrative_text)s,
    from_json(%(citations_json)s, 'array<string>'),
    %(recommendation)s,
    %(tool_trace)s,
    %(judge_verdict)s,
    %(judge_confidence)s,
    %(prompt_version)s,
    %(model_deployment)s,
    %(decided_at)s
"""


class Layer3DecisionsWriter:
    """Writes ``Layer3Decision`` rows to ``audit_dev.gold.layer3_decisions``.

    Pass a zero-arg ``conn_factory`` callable that returns a
    context-managed Databricks SQL connection. Production wires it to
    ``databricks.sql.connect``; tests wire it to a mock.

    Append-only by ``investigation_run_id``; idempotency must be
    enforced upstream (the supervisor mints a fresh ULID per call).
    """

    def __init__(
        self,
        conn_factory: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        self._conn_factory = conn_factory

    @traced_function("layer3.decisions_writer.write_decision")
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=10),
        reraise=True,
    )
    def write_decision(self, decision: Layer3Decision) -> None:
        """INSERT one decision row into ``audit_dev.gold.layer3_decisions``.

        Append-only: the supervisor mints a fresh ULID-style
        ``investigation_run_id`` per call, so no upsert semantics are
        needed (and re-runs on the same scope are intentionally
        preserved as parallel rows).
        """
        params = self._build_params(decision)
        with self._conn_factory() as conn, conn.cursor() as cur:
            cur.execute(_INSERT_SQL, params)

    @staticmethod
    def _build_params(decision: Layer3Decision) -> dict[str, Any]:
        """Map a ``Layer3Decision`` to the INSERT statement's named
        parameters. ``citations`` (array<string>) is serialised to JSON
        for the in-statement ``from_json(..., 'array<string>')`` cast.
        """
        return {
            "agent_run_id": decision.agent_run_id,
            "investigation_run_id": decision.investigation_run_id,
            "engagement_id": decision.engagement_id,
            "control_id": decision.control_id,
            "attribute_id": decision.attribute_id,
            "quarter": decision.quarter,
            "exception_type": decision.exception_type,
            "final_verdict": decision.final_verdict,
            "final_confidence": decision.final_confidence,
            "iterations_used": decision.iterations_used,
            "status": decision.status,
            "narrative_text": decision.narrative_text,
            "citations_json": json.dumps(decision.citations),
            "recommendation": decision.recommendation,
            "tool_trace": decision.tool_trace,
            "judge_verdict": decision.judge_verdict,
            "judge_confidence": decision.judge_confidence,
            "prompt_version": decision.prompt_version,
            "model_deployment": decision.model_deployment,
            "decided_at": decision.decided_at,
        }


__all__ = [
    "GOLD_LAYER3_DECISIONS_TABLE",
    "Layer3DecisionsWriter",
]
