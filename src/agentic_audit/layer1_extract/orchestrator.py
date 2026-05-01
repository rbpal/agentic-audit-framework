"""Pass-case orchestrator for Layer 1 deterministic extraction.

Reads bronze rows for one (engagement, control, quarter) triple, runs
the four-or-six attribute checks (4 for DC-2, 6 for DC-9 per
``ATTRIBUTES_PER_CONTROL``), and assembles an `ExtractedEvidence`
record.

Determinism guarantees:

- ``run_id`` and ``extraction_timestamp`` are injectable. Defaults are
  generated lazily when not provided. Tests pin both to assert
  byte-identical outputs across repeated calls.
- The check functions themselves are not invoked with ``datetime.now()``
  — the timestamp is set once at the orchestrator boundary and threaded
  through.
- No randomness, no LLM, no network beyond the bronze reader's SQL
  warehouse hop.

Scope notes for `task_04`:

- TOC reading is **not** wired in here yet. The dispatch entry point
  ``check_attribute(..., toc)`` accepts ``toc`` for forward-compat but
  the stub implementations in ``attribute_checks.py`` ignore it. A real
  ``TOCReader`` lands alongside the real attribute checks in
  ``step_04_task_03``.
- ``_extract_signoffs`` returns placeholder ``SignOff`` objects derived
  from ``ingested_at`` / ``ingested_by``. Real preparer / reviewer cell
  parsing is also part of the ``step_04_task_03`` scope.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from agentic_audit.layer1_extract.attribute_checks import check_attribute
from agentic_audit.layer1_extract.bronze_reader import (
    BronzeReader,
    ExtractionError,
)
from agentic_audit.models.engagement import ControlId, Quarter
from agentic_audit.models.evidence import (
    ATTRIBUTES_PER_CONTROL,
    AttributeId,
    ExtractedEvidence,
    SignOff,
)
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from agentic_audit.layer1_extract.bronze_reader import BronzeWorkpaperRow


def _new_run_id() -> str:
    """Generate a placeholder run id. Step 4 task_03 / Step 9 will
    swap this for a real ULID once ``python-ulid`` is added."""
    return uuid.uuid4().hex.upper()


def _utc_now() -> datetime:
    return datetime.now(UTC)


_SIGNOFF_RE = re.compile(r"^(?P<initials>[A-Z]{2,4})\s*[—-]\s*(?P<date>\d{4}-\d{2}-\d{2})\s*$")


def _parse_signoff_cell(value: str | None) -> tuple[str, datetime] | None:
    """Parse '<INITIALS> — <YYYY-MM-DD>' → (initials, tz-aware UTC datetime).
    Returns None if the cell is None or doesn't match."""
    if value is None:
        return None
    m = _SIGNOFF_RE.match(value.strip())
    if not m:
        return None
    initials = m.group("initials")
    try:
        date = datetime.fromisoformat(m.group("date")).replace(tzinfo=UTC)
    except ValueError:
        return None
    return initials, date


def _extract_signoffs(
    rows: list[BronzeWorkpaperRow],
) -> tuple[SignOff, SignOff]:
    """Parse preparer / reviewer SignOff from the workpaper.

    DC-9 layout: r4 col_01 = preparer, r5 col_01 = reviewer.
    DC-2 layout: r17 col_01 = reviewer; no preparer cell — synthesise
    a placeholder preparer from the ingest metadata. See
    ``step_04_layer1_extraction.md`` § task_03.1 for the design wart
    note.

    Caller (`extract`) guarantees `rows` is non-empty — no defensive
    guard here.
    """
    control_id = rows[0].control_id
    ingest_date = rows[0].ingested_at

    if control_id == "DC-9":
        preparer = _signoff_from_dc9_row(
            rows, row_index=4, role="preparer", fallback_date=ingest_date
        )
        reviewer = _signoff_from_dc9_row(
            rows, row_index=5, role="reviewer", fallback_date=ingest_date
        )
        return preparer, reviewer

    # DC-2: no preparer cell in the workpaper → synthesise.
    reviewer = _signoff_from_dc2_row(rows, row_index=17, fallback_date=ingest_date)
    preparer = SignOff(initials="AU", role="preparer", date=ingest_date)
    return preparer, reviewer


def _signoff_from_dc9_row(
    rows: list[BronzeWorkpaperRow],
    *,
    row_index: int,
    role: str,
    fallback_date: datetime,
) -> SignOff:
    raw = next(
        (
            r.raw_data.get("col_01")
            for r in rows
            if r.sheet_name == "DC-9 Billing" and r.row_index == row_index
        ),
        None,
    )
    parsed = _parse_signoff_cell(raw)
    if parsed is None:
        return SignOff(initials="??", role=role, date=fallback_date)  # type: ignore[arg-type]
    initials, date = parsed
    return SignOff(initials=initials, role=role, date=date)  # type: ignore[arg-type]


def _signoff_from_dc2_row(
    rows: list[BronzeWorkpaperRow],
    *,
    row_index: int,
    fallback_date: datetime,
) -> SignOff:
    raw = next(
        (
            r.raw_data.get("col_01")
            for r in rows
            if r.sheet_name == "DC-2 Variance" and r.row_index == row_index
        ),
        None,
    )
    parsed = _parse_signoff_cell(raw)
    if parsed is None:
        return SignOff(initials="??", role="reviewer", date=fallback_date)
    initials, date = parsed
    return SignOff(initials=initials, role="reviewer", date=date)


@traced_function("layer1.extract")
def extract(
    engagement_id: str,
    control_id: ControlId,
    quarter: Quarter,
    *,
    bronze_reader: BronzeReader,
    run_id: str | None = None,
    extraction_timestamp: datetime | None = None,
    toc: Any = None,
) -> ExtractedEvidence:
    """Run Layer 1 extraction for one (engagement, control, quarter).

    Returns a fully-validated ``ExtractedEvidence`` with one
    ``AttributeCheck`` per ID in ``ATTRIBUTES_PER_CONTROL[control_id]``
    (4 for DC-2, 6 for DC-9).

    Raises ``ExtractionError`` if the bronze reader returns no rows for
    the triple — that's a real bug (Step 3 ingest didn't run, or the
    triple genuinely doesn't exist), not something the orchestrator
    silently papers over.
    """
    run_id = run_id or _new_run_id()
    extraction_timestamp = extraction_timestamp or _utc_now()

    rows = bronze_reader.read(engagement_id, control_id, quarter)
    if not rows:
        raise ExtractionError(
            f"no bronze rows for ({engagement_id!r}, {control_id!r}, {quarter!r}); "
            "did step_03_task_09 smoke ingest run?"
        )

    attributes = [
        check_attribute(control_id, cast(AttributeId, attr_id), rows, toc)
        for attr_id in ATTRIBUTES_PER_CONTROL[control_id]
    ]

    preparer, reviewer = _extract_signoffs(rows)

    return ExtractedEvidence(
        engagement_id=engagement_id,
        control_id=control_id,
        quarter=quarter,
        run_id=run_id,
        extraction_timestamp=extraction_timestamp,
        preparer=preparer,
        reviewer=reviewer,
        attributes=attributes,
        source_bronze_file_hash=rows[0].file_hash,
        source_path=rows[0].source_path,
    )


__all__ = ["extract"]
