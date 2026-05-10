"""Gold-layer writer for Layer 2 cost telemetry.

Writes one ``CostTelemetry`` row to ``audit_dev.gold.cost_telemetry``
per call. Idempotent on ``agent_run_id``; re-running with the same id
overwrites the prior row, so a sweep that re-emits its summary doesn't
accumulate duplicates.

Mirrors ``GoldNarrativeWriter`` exactly:

- ``conn_factory`` is dependency-injected so tests mock the connector.
- The MERGE statement uses parameter markers (``%(name)s``) for every
  value — Databricks SQL allows them in DML.
- Tenacity retries the whole ``write_cost_telemetry`` on any exception,
  capped at 3 attempts with ``reraise=True``.
- ``@traced_function`` emits span_start / span_end records on the
  ``agentic_audit.trace`` logger so the write is observable in the
  same flight-recorder shape as the other Layer-2 entry points.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tenacity import retry, stop_after_attempt, wait_exponential

from agentic_audit.models.telemetry import CostTelemetry
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager


GOLD_COST_TELEMETRY_TABLE = "audit_dev.gold.cost_telemetry"


# Single-statement MERGE on agent_run_id. The USING subquery
# materialises one virtual row from the parameter markers; the ON
# clause matches the natural primary key. ``cost_usd`` is nullable —
# unknown deployments persist their token + latency data with a NULL
# cost rather than dropping the row.
_MERGE_SQL = f"""
MERGE INTO {GOLD_COST_TELEMETRY_TABLE} AS t
USING (
    SELECT
        %(agent_run_id)s    AS agent_run_id,
        %(input_tokens)s    AS input_tokens,
        %(output_tokens)s   AS output_tokens,
        %(total_tokens)s    AS total_tokens,
        %(latency_ms)s      AS latency_ms,
        %(cost_usd)s        AS cost_usd,
        %(model_version)s   AS model_version,
        %(started_at)s      AS started_at,
        %(completed_at)s    AS completed_at
) AS s
   ON t.agent_run_id = s.agent_run_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""


class CostTelemetryWriter:
    """Writes ``CostTelemetry`` rows to ``audit_dev.gold.cost_telemetry``.

    Pass a zero-arg ``conn_factory`` callable that returns a
    context-managed Databricks SQL connection. Production wires it to
    ``databricks.sql.connect``; tests wire it to a mock.
    """

    def __init__(
        self,
        conn_factory: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        self._conn_factory = conn_factory

    @traced_function("layer2.cost_writer.write_cost_telemetry")
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=10),
        reraise=True,
    )
    def write_cost_telemetry(self, telemetry: CostTelemetry) -> None:
        """MERGE one telemetry row into ``audit_dev.gold.cost_telemetry``.

        Idempotent on ``agent_run_id``; re-running with the same id
        overwrites the prior row.
        """
        params = self._build_params(telemetry)
        with self._conn_factory() as conn, conn.cursor() as cur:
            cur.execute(_MERGE_SQL, params)

    @staticmethod
    def _build_params(telemetry: CostTelemetry) -> dict[str, Any]:
        """Map a ``CostTelemetry`` to the MERGE statement's named
        parameters. Scalars only — no array fields on this table."""
        return {
            "agent_run_id": telemetry.agent_run_id,
            "input_tokens": telemetry.input_tokens,
            "output_tokens": telemetry.output_tokens,
            "total_tokens": telemetry.total_tokens,
            "latency_ms": telemetry.latency_ms,
            "cost_usd": telemetry.cost_usd,
            "model_version": telemetry.model_version,
            "started_at": telemetry.started_at,
            "completed_at": telemetry.completed_at,
        }


__all__ = [
    "GOLD_COST_TELEMETRY_TABLE",
    "CostTelemetryWriter",
]
