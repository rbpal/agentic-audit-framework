"""Gold narratives reader for Layer 2 (Step 6 task_04 cycle D).

Reads ``audit_dev.gold.narratives`` filtered by
``(engagement_id, prompt_version)`` and yields the rows back as
``AttributeNarrative`` instances so the Step 6 judge sweep can iterate
the existing 32 narratives and run ``Judge.evaluate(...)`` per row.

Why this exists: ``GoldNarrativeWriter`` writes; nothing in the codebase
reads ``gold.narratives`` back as pydantic objects. The judge sweep
needs that read path; so will Step 7's supervisor when it wants to
present a narrative + verdict to a human reviewer.

Design mirrors ``SilverEvidenceReader``:

- Zero-arg ``conn_factory`` callable returning a context-managed
  Databricks SQL connection — DI for tests.
- Named-parameter SQL — no f-string value interpolation.
- ``ORDER BY (control_id, quarter, attribute_id)`` for deterministic
  iteration order across sweeps.
- Tenacity retry on the whole ``iter_narratives`` call (3 attempts,
  exponential backoff, ``reraise=True``).
- Empty result → ``GoldNarrativesReadError``: a real sweep should
  always find rows; an empty result means upstream Layer 2 never ran
  for this engagement, or ran under a different ``prompt_version``.
  Failing loud is the breadcrumb.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from tenacity import retry, stop_after_attempt, wait_exponential

from agentic_audit.models.engagement import ControlId, Quarter
from agentic_audit.models.evidence import AttributeId
from agentic_audit.models.narrative import AttributeNarrative
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager


# ---- Errors -------------------------------------------------------------


class GoldNarrativesReadError(RuntimeError):
    """Raised by ``GoldNarrativesReader`` when no narratives match the
    requested ``(engagement_id, prompt_version)``.

    Distinct from a transient DB error (tenacity retries those) so
    callers can ``except GoldNarrativesReadError`` cleanly to handle
    the "Layer 2 hasn't been run yet" case.
    """


GOLD_NARRATIVES_TABLE = "audit_dev.gold.narratives"


# Column ordering here is the contract for ``_row_to_narrative``.
# Don't reorder without updating that method.
_SELECT_SQL = f"""
SELECT engagement_id,
       control_id,
       quarter,
       attribute_id,
       prompt_version,
       source_evidence_id,
       narrative_text,
       cited_fields,
       word_count,
       model_deployment,
       generation_run_id,
       generated_at,
       fact_check_passed,
       fact_check_issues
FROM   {GOLD_NARRATIVES_TABLE}
WHERE  engagement_id  = %(engagement_id)s
  AND  prompt_version = %(prompt_version)s
ORDER  BY control_id, quarter, attribute_id
"""


class GoldNarrativesReader:
    """Reads ``audit_dev.gold.narratives`` and yields ``AttributeNarrative``.

    Pass a zero-arg ``conn_factory`` callable that returns a
    context-managed Databricks SQL connection. Production wires it to
    ``databricks.sql.connect``; tests wire it to a mock.
    """

    def __init__(
        self,
        conn_factory: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        self._conn_factory = conn_factory

    @traced_function("layer2.gold_narratives_reader.iter_narratives")
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=10),
        reraise=True,
    )
    def iter_narratives(
        self,
        engagement_id: str,
        prompt_version: str = "v1.0",
    ) -> list[AttributeNarrative]:
        """Yield every gold narrative matching the engagement +
        prompt_version filter, ordered by (control_id, quarter,
        attribute_id) for deterministic sweep iteration.

        Returns a fully-materialised list (not a generator) so the
        tenacity retry semantics can guard the whole fetch — a
        partially-consumed cursor in a retry path would be ugly.

        Raises ``GoldNarrativesReadError`` if no rows match.
        """
        params = {"engagement_id": engagement_id, "prompt_version": prompt_version}
        with self._conn_factory() as conn, conn.cursor() as cur:
            cur.execute(_SELECT_SQL, params)
            rows = cur.fetchall()

        if not rows:
            raise GoldNarrativesReadError(
                f"no narratives in {GOLD_NARRATIVES_TABLE} for "
                f"(engagement_id={engagement_id!r}, "
                f"prompt_version={prompt_version!r}); has "
                "scripts/run_layer2.py been run for this engagement?"
            )

        return [self._row_to_narrative(r) for r in rows]

    @staticmethod
    def _row_to_narrative(r: Any) -> AttributeNarrative:
        """Map one SELECT row (column order pinned above) to an
        ``AttributeNarrative``. Both array<string> columns
        (``cited_fields``, ``fact_check_issues``) arrive as Python
        lists from the Databricks SQL driver — no JSON parsing needed.
        """
        return AttributeNarrative(
            engagement_id=r[0],
            control_id=cast(ControlId, r[1]),
            quarter=cast(Quarter, r[2]),
            attribute_id=cast(AttributeId, r[3]),
            prompt_version=r[4],
            source_evidence_id=r[5],
            narrative_text=r[6],
            cited_fields=list(r[7]) if r[7] is not None else [],
            word_count=r[8],
            model_deployment=r[9],
            generation_run_id=r[10],
            generated_at=r[11],
            fact_check_passed=r[12],
            fact_check_issues=list(r[13]) if r[13] is not None else [],
        )


__all__ = [
    "GOLD_NARRATIVES_TABLE",
    "GoldNarrativesReader",
    "GoldNarrativesReadError",
]
