"""Layer 2 sweep iterator — the 32 narratable combinations.

Pure-function `iter_narratable_combinations` enumerates every
``(engagement_id, control_id, quarter, attribute_id)`` tuple that
Layer 2 should produce a narrative for, derived from the
``NARRATABLE_ATTRIBUTES_PER_CONTROL`` constant in
``models/evidence.py``.

For ``alpha-pension-fund-2025`` (8 audit periods × narratable
attribute count per control):

- DC-2 × {A, C, D} × {Q1, Q2, Q3, Q4} = 12 tuples
- DC-9 × {A, B, C, E, F} × {Q1, Q2, Q3, Q4} = 20 tuples
- Total: 32 tuples

Layer 3 attributes (DC-2.B and DC-9.D) are excluded by construction
— they live in ``LAYER3_ATTRIBUTES_PER_CONTROL`` and are reserved
for the React-loop multi-agent path.

Used by ``scripts/run_layer2.py``; pure for unit-testability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentic_audit.models.engagement import ControlId, Quarter
from agentic_audit.models.evidence import (
    NARRATABLE_ATTRIBUTES_PER_CONTROL,
    AttributeId,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

# Quarters under audit. Order is deterministic (Q1 → Q4) so sweep
# logs and gold rows land in a predictable sequence.
_QUARTERS: tuple[Quarter, ...] = ("Q1", "Q2", "Q3", "Q4")

# Controls in scope. Source of truth is keys of
# NARRATABLE_ATTRIBUTES_PER_CONTROL; pinned here for ordering.
_CONTROLS: tuple[ControlId, ...] = ("DC-2", "DC-9")


def iter_narratable_combinations(
    *,
    engagement_id: str,
) -> Iterator[tuple[str, ControlId, Quarter, AttributeId]]:
    """Yield every narratable combination for a single engagement.

    Order: by control (DC-2 → DC-9), then by quarter (Q1 → Q4),
    then by attribute (alphabetical). Stable for log-grep and
    deterministic gold-table population order.
    """
    for control in _CONTROLS:
        narratable = NARRATABLE_ATTRIBUTES_PER_CONTROL[control]
        for quarter in _QUARTERS:
            for attribute in narratable:
                yield (engagement_id, control, quarter, attribute)  # type: ignore[misc]


__all__ = [
    "iter_narratable_combinations",
]
