"""Per-control attribute checks (STUB — real logic in step_04_task_03).

This file ships in step_04_task_04 (orchestrator) so the orchestrator
has something to import and call. Every check returns
``status="pass"`` with empty evidence_cell_refs and a marker note. The
real per-(control, attribute) check logic — 10 branches reading bronze
rows + TOC fields — lands in step_04_task_03 once §5.3.1 of the spec
doc is filled in.

The dispatch shape is final: a single ``check_attribute()`` entry point
that branches on ``(control_id, attribute_id)`` to a per-attribute
implementation. task_03 replaces the inner functions; everything else
(signature, error handling, control coverage) stays.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentic_audit.models.engagement import ControlId
from agentic_audit.models.evidence import (
    ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
    AttributeId,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentic_audit.layer1_extract.bronze_reader import BronzeWorkpaperRow

    _CheckImpl = Callable[[list["BronzeWorkpaperRow"], Any], AttributeCheck]


_STUB_NOTE = "task_03 stub: real check logic pending TOC mapping"


def _stub_pass(
    control_id: ControlId,
    attribute_id: AttributeId,
) -> AttributeCheck:
    return AttributeCheck(
        control_id=control_id,
        attribute_id=attribute_id,
        status="pass",
        evidence_cell_refs=[],
        notes=_STUB_NOTE,
    )


def _dc2_a(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    return _stub_pass("DC-2", "A")


def _dc2_b(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    return _stub_pass("DC-2", "B")


def _dc2_c(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    return _stub_pass("DC-2", "C")


def _dc2_d(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    return _stub_pass("DC-2", "D")


def _dc9_a(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    return _stub_pass("DC-9", "A")


def _dc9_b(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    return _stub_pass("DC-9", "B")


def _dc9_c(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    return _stub_pass("DC-9", "C")


def _dc9_d(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    return _stub_pass("DC-9", "D")


def _dc9_e(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    return _stub_pass("DC-9", "E")


def _dc9_f(rows: list[BronzeWorkpaperRow], toc: Any) -> AttributeCheck:
    return _stub_pass("DC-9", "F")


_DISPATCH: dict[tuple[ControlId, AttributeId], _CheckImpl] = {
    ("DC-2", "A"): _dc2_a,
    ("DC-2", "B"): _dc2_b,
    ("DC-2", "C"): _dc2_c,
    ("DC-2", "D"): _dc2_d,
    ("DC-9", "A"): _dc9_a,
    ("DC-9", "B"): _dc9_b,
    ("DC-9", "C"): _dc9_c,
    ("DC-9", "D"): _dc9_d,
    ("DC-9", "E"): _dc9_e,
    ("DC-9", "F"): _dc9_f,
}


def check_attribute(
    control_id: ControlId,
    attribute_id: AttributeId,
    rows: list[BronzeWorkpaperRow],
    toc: Any,
) -> AttributeCheck:
    """Dispatch entry point.

    Looks up the implementation for ``(control_id, attribute_id)`` and
    calls it. Raises ``KeyError`` if the pair is not registered (e.g.
    DC-2 doesn't define attribute E).
    """
    if attribute_id not in ATTRIBUTES_PER_CONTROL[control_id]:
        raise KeyError(
            f"control_id={control_id} does not define attribute_id={attribute_id}; "
            f"valid attributes for {control_id}: {ATTRIBUTES_PER_CONTROL[control_id]}"
        )
    return _DISPATCH[(control_id, attribute_id)](rows, toc)


__all__ = ["check_attribute"]
