"""Silver evidence reader for Layer 2 (Decision 4b).

Reads ``audit_dev.silver.evidence`` filtered by
``(engagement_id, control_id, quarter)`` and reconstructs the full
``ExtractedEvidence`` record. The 4-or-6 silver rows that share an
``(engagement, control, quarter)`` triple all carry the same envelope
(``run_id``, ``preparer_*``, ``reviewer_*``) — we read it from the first
row and fold the per-attribute ``narrative`` JSON back into
``AttributeCheck`` instances, one per row.

Why this exists (Decision 4b in
``privateDocs/step_05_layer2_narrative.md``): silver is the stable
contract between Layer 1 and downstream consumers. Layer 2 reads from
silver — *not* by re-running Layer 1's ``extract()`` in process — so
that any future change to Layer 1's in-memory shape forces a Terraform
migration and an explicit conversation, instead of breaking Layer 2
silently.

Why the round-trip is faithful (rather than lossy): step_05_task_02a
extended ``silver.evidence`` with the envelope columns so that
``SilverEvidenceReader.read(...) -> ExtractedEvidence`` actually works
— same shape Layer 1 produced, no synthesised fields, no asymmetric
parsing.

Mirrors ``BronzeReader`` shape: zero-arg ``conn_factory`` callable
returning a context-managed Databricks SQL connection (lazy-imported
at the wiring layer, not here, so unit tests can mock the factory
without ``databricks-sql-connector`` installed); tenacity retry on
the whole ``read()`` call.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from tenacity import retry, stop_after_attempt, wait_exponential

from agentic_audit.models.engagement import ControlId, Quarter
from agentic_audit.models.evidence import (
    AttributeCheck,
    AttributeId,
    CheckStatus,
    ExtractedEvidence,
    SignOff,
)
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager


# ---- Errors -------------------------------------------------------------


class SilverReadError(RuntimeError):
    """Raised by ``SilverEvidenceReader`` when reconstruction cannot
    proceed: no rows for the triple, missing envelope columns
    (pre-migration data), or malformed ``narrative`` JSON.

    Distinct from pydantic ``ValidationError`` so callers can
    ``except SilverReadError`` cleanly without catching unrelated
    validation failures.
    """


# ---- SQL ----------------------------------------------------------------


# Column ordering here is the contract for ``_row_to_attribute_check`` and
# the envelope unpack below. Don't reorder without updating both.
_SELECT_SQL = """
SELECT engagement_id,
       control_id,
       attribute_id,
       quarter,
       source_path,
       source_file_hash,
       narrative,
       ingested_at,
       run_id,
       preparer_initials,
       preparer_role,
       preparer_date,
       reviewer_initials,
       reviewer_role,
       reviewer_date
FROM audit_dev.silver.evidence
WHERE engagement_id = %(eng)s
  AND control_id    = %(ctrl)s
  AND quarter       = %(q)s
ORDER BY attribute_id
"""


# ---- Reader -------------------------------------------------------------


class SilverEvidenceReader:
    """Reads silver rows and reconstructs ``ExtractedEvidence``.

    Pass a zero-arg ``conn_factory`` callable that returns a
    context-managed Databricks SQL connection. Production wires it to
    ``databricks.sql.connect``; tests wire it to a mock.
    """

    def __init__(
        self,
        conn_factory: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        self._conn_factory = conn_factory

    @traced_function("layer2.silver_reader.read")
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=10),
        reraise=True,
    )
    def read(
        self,
        engagement_id: str,
        control_id: ControlId,
        quarter: Quarter,
    ) -> ExtractedEvidence:
        """Reconstruct ``ExtractedEvidence`` from silver rows for one
        ``(engagement, control, quarter)`` triple.

        Returns a fully-validated ``ExtractedEvidence`` with one
        ``AttributeCheck`` per silver row (4 for DC-2, 6 for DC-9).

        Raises ``SilverReadError`` if no rows match the triple — silver
        should have been populated by a prior Layer 1 ``extract()`` for
        any valid combination, so an empty result is a real bug
        (extract never ran, or ran for a different engagement) rather
        than something the reader silently papers over.
        """
        params = {"eng": engagement_id, "ctrl": control_id, "q": quarter}
        with self._conn_factory() as conn, conn.cursor() as cur:
            cur.execute(_SELECT_SQL, params)
            rows = cur.fetchall()

        if not rows:
            raise SilverReadError(
                f"no silver rows for ({engagement_id!r}, {control_id!r}, "
                f"{quarter!r}); did Layer 1 extract run for this triple? "
                "(scripts/run_layer1.py repopulates silver from bronze.)"
            )

        # Envelope columns are duplicated across all rows for the triple
        # by silver_writer's _explode_to_silver_rows. Take them from the
        # first row.
        first = rows[0]
        source_path = first[4]
        source_file_hash = first[5]
        ingested_at = first[7]
        run_id = first[8]
        preparer_initials = first[9]
        preparer_role = first[10]
        preparer_date = first[11]
        reviewer_initials = first[12]
        reviewer_role = first[13]
        reviewer_date = first[14]

        if any(
            v is None
            for v in (
                run_id,
                preparer_initials,
                preparer_role,
                preparer_date,
                reviewer_initials,
                reviewer_role,
                reviewer_date,
            )
        ):
            raise SilverReadError(
                f"silver row for ({engagement_id!r}, {control_id!r}, "
                f"{quarter!r}) is missing envelope columns "
                "(run_id / preparer_* / reviewer_*); row likely pre-dates "
                "step_05_task_02a migration. Re-run scripts/run_layer1.py "
                "to repopulate."
            )

        attributes = [self._row_to_attribute_check(r) for r in rows]

        return ExtractedEvidence(
            engagement_id=engagement_id,
            control_id=control_id,
            quarter=quarter,
            run_id=run_id,
            extraction_timestamp=ingested_at,
            preparer=SignOff(
                initials=preparer_initials,
                role=preparer_role,
                date=preparer_date,
            ),
            reviewer=SignOff(
                initials=reviewer_initials,
                role=reviewer_role,
                date=reviewer_date,
            ),
            attributes=attributes,
            source_bronze_file_hash=source_file_hash,
            source_path=source_path,
        )

    @staticmethod
    def _row_to_attribute_check(r: Any) -> AttributeCheck:
        """Parse the ``narrative`` JSON column back into an
        ``AttributeCheck``.

        The ``narrative`` column was written by silver_writer's
        ``_attribute_check_to_narrative`` which serialises
        ``(status, evidence_cell_refs, extracted_value, notes)``.
        ``control_id`` and ``attribute_id`` come from the dedicated
        silver columns, not the JSON.
        """
        control_id = r[1]
        attribute_id = r[2]
        narrative_json = r[6]
        try:
            payload = json.loads(narrative_json)
        except (json.JSONDecodeError, TypeError) as e:
            raise SilverReadError(
                f"malformed narrative JSON in silver row "
                f"(control={control_id!r}, attribute={attribute_id!r}): {e}"
            ) from e
        return AttributeCheck(
            control_id=cast(ControlId, control_id),
            attribute_id=cast(AttributeId, attribute_id),
            status=cast(CheckStatus, payload["status"]),
            evidence_cell_refs=payload.get("evidence_cell_refs", []),
            extracted_value=payload.get("extracted_value"),
            notes=payload.get("notes"),
        )


__all__ = [
    "SilverEvidenceReader",
    "SilverReadError",
]
