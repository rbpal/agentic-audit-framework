"""Gold-layer writer for Layer 2 narratives.

Writes one ``AttributeNarrative`` to ``audit_dev.gold.narratives`` per
call. Idempotent — re-running with the same composite key
``(engagement_id, control_id, quarter, attribute_id, prompt_version)``
overwrites the prior row rather than accumulating. Bumping any
component of the key — most usefully ``prompt_version`` — inserts a
parallel row, which is what enables A/B comparison of prompt versions
against the same evidence.

Design follows the Layer 1 ``SilverWriter`` pattern:

- ``conn_factory`` is dependency-injected so tests mock the connector
  while production wires it to ``databricks.sql.connect``.
- The MERGE statement uses parameter markers (``%(name)s``) for every
  value — Databricks SQL allows them in DML even though it forbids
  them in DDL. No f-string interpolation of values into SQL.
- ``cited_fields`` and ``fact_check_issues`` (both ``array<string>``)
  are passed as JSON strings and cast in-statement via
  ``from_json(..., 'array<string>')``. Variable-length arrays via
  named-parameter VALUES are otherwise awkward; the JSON cast is the
  cleanest portable shape.
- Tenacity retries the whole ``write_narrative`` on any exception, capped
  at 3 attempts with ``reraise=True`` so callers see the original
  exception class.

See ``privateDocs/step_05_layer2_narrative.md`` ``step_05_task_06``
pre-execution notes for the table schema, key contract, and rejected
alternatives (gating fact-check failures, separate sidecar verdict
table, batched MERGE).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from tenacity import retry, stop_after_attempt, wait_exponential

from agentic_audit.models.narrative import AttributeNarrative
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager


GOLD_NARRATIVES_TABLE = "audit_dev.gold.narratives"


# Single-statement MERGE. The USING subquery materialises one virtual
# row from the parameter markers; the ON clause matches the composite
# primary key; arrays are cast via from_json so the parameter binding
# can stay simple JSON strings.
_MERGE_SQL = f"""
MERGE INTO {GOLD_NARRATIVES_TABLE} AS t
USING (
    SELECT
        %(engagement_id)s          AS engagement_id,
        %(control_id)s             AS control_id,
        %(quarter)s                AS quarter,
        %(attribute_id)s           AS attribute_id,
        %(prompt_version)s         AS prompt_version,
        %(source_evidence_id)s     AS source_evidence_id,
        %(narrative_text)s         AS narrative_text,
        from_json(%(cited_fields_json)s, 'array<string>')      AS cited_fields,
        %(word_count)s             AS word_count,
        %(model_deployment)s       AS model_deployment,
        %(generation_run_id)s      AS generation_run_id,
        %(narrative_call_id)s      AS narrative_call_id,
        %(generated_at)s           AS generated_at,
        %(fact_check_passed)s      AS fact_check_passed,
        from_json(%(fact_check_issues_json)s, 'array<string>') AS fact_check_issues
) AS s
   ON  t.engagement_id   = s.engagement_id
  AND t.control_id      = s.control_id
  AND t.quarter         = s.quarter
  AND t.attribute_id    = s.attribute_id
  AND t.prompt_version  = s.prompt_version
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""


class GoldNarrativeWriter:
    """Writes ``AttributeNarrative`` records to ``audit_dev.gold.narratives``.

    Pass a zero-arg ``conn_factory`` callable that returns a
    context-managed Databricks SQL connection. Production wires it to
    ``databricks.sql.connect``; tests wire it to a mock.
    """

    def __init__(
        self,
        conn_factory: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        self._conn_factory = conn_factory

    @traced_function("layer2.gold_writer.write_narrative")
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=10),
        reraise=True,
    )
    def write_narrative(self, narrative: AttributeNarrative) -> None:
        """MERGE one narrative into ``audit_dev.gold.narratives``.

        Idempotent on the composite key
        ``(engagement_id, control_id, quarter, attribute_id, prompt_version)``;
        re-running with the same key overwrites the prior row.
        """
        params = self._build_params(narrative)
        with self._conn_factory() as conn, conn.cursor() as cur:
            cur.execute(_MERGE_SQL, params)

    @staticmethod
    def _build_params(narrative: AttributeNarrative) -> dict[str, Any]:
        """Map an ``AttributeNarrative`` to the MERGE statement's named
        parameters. ``cited_fields`` and ``fact_check_issues`` are
        serialised to JSON strings for the in-statement
        ``from_json(..., 'array<string>')`` cast.
        """
        return {
            "engagement_id": narrative.engagement_id,
            "control_id": narrative.control_id,
            "quarter": narrative.quarter,
            "attribute_id": narrative.attribute_id,
            "prompt_version": narrative.prompt_version,
            "source_evidence_id": narrative.source_evidence_id,
            "narrative_text": narrative.narrative_text,
            "cited_fields_json": json.dumps(narrative.cited_fields),
            "word_count": narrative.word_count,
            "model_deployment": narrative.model_deployment,
            "generation_run_id": narrative.generation_run_id,
            "narrative_call_id": narrative.narrative_call_id,
            "generated_at": narrative.generated_at,
            "fact_check_passed": narrative.fact_check_passed,
            "fact_check_issues_json": json.dumps(narrative.fact_check_issues),
        }


__all__ = [
    "GOLD_NARRATIVES_TABLE",
    "GoldNarrativeWriter",
]
