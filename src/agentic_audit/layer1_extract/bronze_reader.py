"""Bronze reader for Layer 1 (Design B).

Reads workpaper rows from `audit_dev.bronze.workpapers_raw` filtered by
`(engagement_id, control_id, quarter)`. The bronze table itself does
not carry `control_id` / `quarter` columns — those are encoded in the
`source_path` (e.g. ``.../dc9_Q1_ref.xlsx``). The reader pushes a
``LIKE`` filter on `source_path` down to Spark and parses the path on
the way out so callers see structured fields.

Design notes:

- ``BronzeReader.__init__`` takes a zero-arg ``conn_factory`` callable
  that returns a context-managed Databricks SQL connection. This keeps
  the reader free of any direct ``databricks-sql-connector`` import,
  which means unit tests can mock the factory without the package
  installed.
- Tenacity retries the whole ``read()`` call on any exception, capped
  at 3 attempts with exponential backoff. Most transient SQL warehouse
  errors (cold-start delays, brief network blips) recover within one
  retry; a permanent error (auth, syntax) burns through the budget
  quickly enough.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from agentic_audit.models.engagement import ControlId, Quarter
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager


# ---- Path parsing -------------------------------------------------------

_PATH_RE = re.compile(r"dc(?P<num>\d+)_(?P<quarter>Q[1-4])", re.IGNORECASE)


def parse_control_quarter_from_path(source_path: str) -> tuple[ControlId, Quarter]:
    """Extract `(control_id, quarter)` from a workpaper path.

    Supports paths like::

        abfss://bronze@<storage>.dfs.core.windows.net/corpus/v2/workpapers/dc9_Q1_ref.xlsx
        /Volumes/audit_dev/bronze/raw_pdfs/corpus/v2/workpapers/dc2_Q3_ref.xlsx

    Raises ``ExtractionError`` if the pattern is missing.
    """
    m = _PATH_RE.search(source_path)
    if not m:
        raise ExtractionError(
            f"cannot parse (control_id, quarter) from source_path={source_path!r}"
        )
    control_id = f"DC-{m.group('num')}"
    quarter = m.group("quarter").upper()
    if control_id not in ("DC-2", "DC-9"):
        raise ExtractionError(f"unknown control_id={control_id!r} parsed from {source_path!r}")
    return control_id, quarter  # type: ignore[return-value]


# ---- Models -------------------------------------------------------------


class BronzeWorkpaperRow(BaseModel):
    """One row from `audit_dev.bronze.workpapers_raw`, augmented with
    `control_id` and `quarter` parsed from `source_path`.

    ``ingestion_id`` is ``Optional[int]`` because the bronze table's
    Terraform definition declares the column as a plain nullable
    ``bigint`` (the comment "Auto-incremented per ingestion event" was
    aspirational — the column is NOT
    ``GENERATED ALWAYS AS IDENTITY`` and the smoke ingest does not
    populate it explicitly). The model reflects what bronze actually
    carries, not what we wished it carried. Downstream consumers
    (attribute_checks, orchestrator) do not read ``ingestion_id`` —
    only ``sheet_name``, ``row_index`` and ``raw_data`` matter for
    extraction.
    """

    ingestion_id: int | None = None
    source_path: str
    file_hash: str
    engagement_id: str
    control_id: ControlId
    quarter: Quarter
    sheet_name: str
    row_index: int
    raw_data: dict[str, str] = Field(default_factory=dict)
    ingested_at: datetime
    ingested_by: str


# ---- Errors -------------------------------------------------------------


class ExtractionError(RuntimeError):
    """Raised by Layer 1 when extraction cannot proceed (no rows, malformed
    path, downstream contract violation). Distinct from pydantic
    ``ValidationError`` so callers can ``except ExtractionError`` cleanly."""


# ---- Reader -------------------------------------------------------------


_SELECT_SQL = """
SELECT ingestion_id,
       source_path,
       file_hash,
       engagement_id,
       sheet_name,
       row_index,
       raw_data,
       ingested_at,
       ingested_by
FROM audit_dev.bronze.workpapers_raw
WHERE engagement_id = %(eng)s
  AND source_path LIKE %(path_like)s
ORDER BY source_path, sheet_name, row_index
"""


class BronzeReader:
    """Reads workpaper rows from `audit_dev.bronze.workpapers_raw`.

    Pass a zero-arg `conn_factory` callable that returns a context-managed
    Databricks SQL connection. Production wires it to
    ``databricks.sql.connect``; tests wire it to a mock.
    """

    def __init__(
        self,
        conn_factory: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        self._conn_factory = conn_factory

    @traced_function("layer1.bronze_reader.read")
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
    ) -> list[BronzeWorkpaperRow]:
        """Return all bronze rows for the given (engagement, control, quarter).

        Filters via SQL pushdown on `source_path LIKE '%dcN_Qn_%'`.
        Rows come back ordered by `(source_path, sheet_name, row_index)`.
        """
        path_like = f"%dc{control_id.split('-')[1]}_{quarter}_%"
        params = {"eng": engagement_id, "path_like": path_like}

        with self._conn_factory() as conn, conn.cursor() as cur:
            cur.execute(_SELECT_SQL, params)
            rows = cur.fetchall()

        return [self._row_to_model(r) for r in rows]

    @staticmethod
    def _row_to_model(r: Any) -> BronzeWorkpaperRow:
        """Map a Databricks SQL row (tuple, Row, or dict-like) to the
        pydantic model. Source_path is parsed for control_id + quarter."""
        # databricks-sql-connector returns Row objects supporting both
        # tuple-indexing and attribute access. Treat them as sequence.
        (
            ingestion_id,
            source_path,
            file_hash,
            engagement_id,
            sheet_name,
            row_index,
            raw_data,
            ingested_at,
            ingested_by,
        ) = (
            r[0],
            r[1],
            r[2],
            r[3],
            r[4],
            r[5],
            r[6],
            r[7],
            r[8],
        )
        control_id, quarter = parse_control_quarter_from_path(source_path)
        return BronzeWorkpaperRow(
            ingestion_id=int(ingestion_id) if ingestion_id is not None else None,
            source_path=source_path,
            file_hash=file_hash,
            engagement_id=engagement_id,
            control_id=control_id,
            quarter=quarter,
            sheet_name=sheet_name,
            row_index=int(row_index),
            raw_data=dict(raw_data) if raw_data is not None else {},
            ingested_at=ingested_at,
            ingested_by=ingested_by,
        )
