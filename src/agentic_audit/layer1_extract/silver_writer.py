"""Silver writer for Layer 1.

Explodes one `ExtractedEvidence` into one silver row per `AttributeCheck`
(4 rows for DC-2, 6 for DC-9) and merges them into
`audit_dev.silver.evidence`. Idempotent — same `(engagement, control,
attribute, quarter)` overwrites the prior row rather than accumulating.

Design notes:

- `conn_factory` is dependency-injected (same pattern as `BronzeReader`).
  Tests mock the factory; production wires it to `databricks.sql.connect`.
- `evidence_id` is computed as a stable bigint hash of the natural key
  `(engagement, control, attribute, quarter)`. Re-running the writer for
  the same triple produces the same `evidence_id`, so MERGE actually
  replaces (not insert-with-new-id).
- The MERGE statement keys on `(engagement, control, attribute, quarter)`,
  matching the silver-table dedup contract. `evidence_id` is the
  primary key but the natural key is what gates idempotency.
- Tenacity retries the whole `write_evidence` on any exception, capped
  at 3 attempts with `reraise=True` so callers see the original
  exception class.
- `narrative` serializes the `AttributeCheck` as JSON (status,
  evidence_cell_refs, extracted_value, notes). Layer 2 reads this.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from agentic_audit.models.evidence import (
    AttributeCheck,
    ExtractedEvidence,
)
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager


class SilverEvidenceRow(BaseModel):
    """One row in `audit_dev.silver.evidence`. The pydantic guarantees
    every row has a stable shape before it hits the warehouse.

    The trailing `run_id` + `preparer_*` + `reviewer_*` fields are the
    envelope columns added in step_05_task_02a so silver carries the
    full `ExtractedEvidence` shape. They are required at write time
    (Layer 1 always knows them) but the underlying Delta columns are
    nullable to tolerate rows ingested before the migration.
    """

    evidence_id: int
    engagement_id: str
    control_id: str
    attribute_id: str
    quarter: str
    source_path: str
    source_file_hash: str
    evidence_type: str
    narrative: str
    ingested_at: datetime
    # ---- Envelope columns (step_05_task_02a) ------------------------------
    run_id: str
    preparer_initials: str
    preparer_role: str
    preparer_date: datetime
    reviewer_initials: str
    reviewer_role: str
    reviewer_date: datetime


def _evidence_id(engagement_id: str, control_id: str, attribute_id: str, quarter: str) -> int:
    """Stable bigint hash of the natural key.

    Take SHA-256 of the joined key, slice the top 8 bytes, mask off the
    sign bit so the result fits in a signed bigint (Spark's `bigint` is
    Java long = signed 64-bit). Re-running with the same inputs is
    guaranteed to produce the same id, which is what makes MERGE
    overwrite-not-duplicate behave correctly.
    """
    payload = f"{engagement_id},{control_id},{attribute_id},{quarter}".encode()
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big", signed=False) >> 1


def _attribute_check_to_narrative(check: AttributeCheck) -> str:
    """Serialize an AttributeCheck as JSON for the silver `narrative` column."""
    return json.dumps(
        {
            "status": check.status,
            "evidence_cell_refs": check.evidence_cell_refs,
            "extracted_value": check.extracted_value,
            "notes": check.notes,
        },
        default=str,  # handles Decimal, datetime, etc.
        sort_keys=True,
    )


_STAGED_VIEW_SQL = """
CREATE OR REPLACE TEMPORARY VIEW _silver_staged AS
SELECT * FROM (
    VALUES {values_clause}
) AS s (
    evidence_id, engagement_id, control_id, attribute_id, quarter,
    source_path, source_file_hash, evidence_type, narrative, ingested_at,
    run_id, preparer_initials, preparer_role, preparer_date,
    reviewer_initials, reviewer_role, reviewer_date
)
"""

_MERGE_SQL = """
MERGE INTO audit_dev.silver.evidence AS t
USING _silver_staged AS s
   ON  t.engagement_id = s.engagement_id
  AND t.control_id    = s.control_id
  AND t.attribute_id  = s.attribute_id
  AND t.quarter       = s.quarter
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""


class SilverWriter:
    """Writes `ExtractedEvidence` records to `audit_dev.silver.evidence`.

    Pass a zero-arg `conn_factory` callable that returns a context-managed
    Databricks SQL connection. Production wires it to
    `databricks.sql.connect`; tests wire it to a mock.
    """

    def __init__(
        self,
        conn_factory: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        self._conn_factory = conn_factory

    @staticmethod
    def _explode_to_silver_rows(
        record: ExtractedEvidence,
    ) -> list[SilverEvidenceRow]:
        """One silver row per AttributeCheck. 4 rows for DC-2, 6 for DC-9.

        Every row inherits the same envelope (run_id + preparer + reviewer)
        from the parent ExtractedEvidence. The redundancy is intentional —
        silver is queried at the (engagement, control, attribute, quarter)
        grain and downstream readers reconstruct ExtractedEvidence from any
        single row's envelope columns.
        """
        return [
            SilverEvidenceRow(
                evidence_id=_evidence_id(
                    record.engagement_id,
                    record.control_id,
                    check.attribute_id,
                    record.quarter,
                ),
                engagement_id=record.engagement_id,
                control_id=record.control_id,
                attribute_id=check.attribute_id,
                quarter=record.quarter,
                source_path=record.source_path,
                source_file_hash=record.source_bronze_file_hash,
                evidence_type="workpaper-row",
                narrative=_attribute_check_to_narrative(check),
                ingested_at=record.extraction_timestamp,
                # ---- Envelope (step_05_task_02a) --------------------------
                run_id=record.run_id,
                preparer_initials=record.preparer.initials,
                preparer_role=record.preparer.role,
                preparer_date=record.preparer.date,
                reviewer_initials=record.reviewer.initials,
                reviewer_role=record.reviewer.role,
                reviewer_date=record.reviewer.date,
            )
            for check in record.attributes
        ]

    @traced_function("layer1.silver_writer.write_evidence")
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=10),
        reraise=True,
    )
    def write_evidence(self, record: ExtractedEvidence) -> None:
        """MERGE all per-attribute rows for one (engagement, control, quarter)
        into `audit_dev.silver.evidence`. Atomic per record (one MERGE)."""
        rows = self._explode_to_silver_rows(record)
        params, values_clause = self._build_values_clause(rows)
        staged_sql = _STAGED_VIEW_SQL.format(values_clause=values_clause)

        with self._conn_factory() as conn, conn.cursor() as cur:
            cur.execute(staged_sql, params)
            cur.execute(_MERGE_SQL)

    @staticmethod
    def _build_values_clause(
        rows: list[SilverEvidenceRow],
    ) -> tuple[dict[str, Any], str]:
        """Build the `VALUES (...), (...)` clause for the staged view.

        Returns a (params dict, values clause) pair. Each row contributes
        17 named parameters keyed `r{i}_{column}` to avoid collisions —
        10 original silver columns plus 7 envelope columns added in
        step_05_task_02a.
        """
        params: dict[str, Any] = {}
        row_clauses: list[str] = []
        for i, r in enumerate(rows):
            placeholders = ", ".join(
                f"%({key}_{i})s"
                for key in (
                    "evidence_id",
                    "engagement_id",
                    "control_id",
                    "attribute_id",
                    "quarter",
                    "source_path",
                    "source_file_hash",
                    "evidence_type",
                    "narrative",
                    "ingested_at",
                    "run_id",
                    "preparer_initials",
                    "preparer_role",
                    "preparer_date",
                    "reviewer_initials",
                    "reviewer_role",
                    "reviewer_date",
                )
            )
            row_clauses.append(f"({placeholders})")
            params.update(
                {
                    f"evidence_id_{i}": r.evidence_id,
                    f"engagement_id_{i}": r.engagement_id,
                    f"control_id_{i}": r.control_id,
                    f"attribute_id_{i}": r.attribute_id,
                    f"quarter_{i}": r.quarter,
                    f"source_path_{i}": r.source_path,
                    f"source_file_hash_{i}": r.source_file_hash,
                    f"evidence_type_{i}": r.evidence_type,
                    f"narrative_{i}": r.narrative,
                    f"ingested_at_{i}": r.ingested_at,
                    f"run_id_{i}": r.run_id,
                    f"preparer_initials_{i}": r.preparer_initials,
                    f"preparer_role_{i}": r.preparer_role,
                    f"preparer_date_{i}": r.preparer_date,
                    f"reviewer_initials_{i}": r.reviewer_initials,
                    f"reviewer_role_{i}": r.reviewer_role,
                    f"reviewer_date_{i}": r.reviewer_date,
                }
            )
        return params, ", ".join(row_clauses)


__all__ = [
    "SilverEvidenceRow",
    "SilverWriter",
]
