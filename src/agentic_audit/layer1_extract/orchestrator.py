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

if TYPE_CHECKING:
    from agentic_audit.layer1_extract.bronze_reader import BronzeWorkpaperRow


def _new_run_id() -> str:
    """Generate a placeholder run id. Step 4 task_03 / Step 9 will
    swap this for a real ULID once ``python-ulid`` is added."""
    return uuid.uuid4().hex.upper()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _extract_signoffs(
    rows: list[BronzeWorkpaperRow],
) -> tuple[SignOff, SignOff]:
    """Build placeholder preparer / reviewer SignOff objects.

    Real preparer / reviewer cell parsing (reading the workpaper
    Checklist tab rows for initials, role, sign-off date) lands in
    `step_04_task_03`. For the orchestrator wiring + tests, derive
    deterministic placeholder values from the bronze metadata.

    Caller (`extract`) guarantees `rows` is non-empty — no defensive
    guard here.
    """
    signoff_date = rows[0].ingested_at
    return (
        SignOff(initials="P1", role="preparer", date=signoff_date),
        SignOff(initials="R1", role="reviewer", date=signoff_date),
    )


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
    )


__all__ = ["extract"]
