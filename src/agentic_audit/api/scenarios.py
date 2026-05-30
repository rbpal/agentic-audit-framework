"""The load-test scenario catalog.

The Locust users randomly POST a ``scenario_id`` from this catalog. There
are 8 gold scenarios in the v2 corpus: controls DC-2 + DC-9 across quarters
Q1-Q4 of the single engagement ``alpha-pension-fund-2025`` (the master
plan's aspirational "20 scenarios" predates the v2 corpus; only these 8
exist). A scenario maps to the (control, quarter, prior_quarter) the worker
needs to load silver evidence and run an investigation.

Q1 has no prior quarter, so a REAL Layer-3 investigation needs Q2-Q4 as the
*current* quarter; ``REAL_ELIGIBLE_IDS`` filters to those. The MOCK path
(the scaling demo) ignores the pairing entirely and accepts all 8.
"""

from __future__ import annotations

from dataclasses import dataclass

ENGAGEMENT_ID = "alpha-pension-fund-2025"

_PRIOR: dict[str, str | None] = {"Q1": None, "Q2": "Q1", "Q3": "Q2", "Q4": "Q3"}
_CONTROL_PREFIX: dict[str, str] = {"DC-2": "dc2", "DC-9": "dc9"}


@dataclass(frozen=True)
class Scenario:
    """One load-test scenario: a (control, quarter) point in the corpus."""

    scenario_id: str
    engagement_id: str
    control_id: str  # "DC-2" | "DC-9"
    quarter: str  # "Q1".."Q4"
    prior_quarter: str | None


def _build_catalog() -> dict[str, Scenario]:
    catalog: dict[str, Scenario] = {}
    for control_id, prefix in _CONTROL_PREFIX.items():
        for quarter in ("Q1", "Q2", "Q3", "Q4"):
            scenario_id = f"{prefix}_{quarter}"
            catalog[scenario_id] = Scenario(
                scenario_id=scenario_id,
                engagement_id=ENGAGEMENT_ID,
                control_id=control_id,
                quarter=quarter,
                prior_quarter=_PRIOR[quarter],
            )
    return catalog


SCENARIOS: dict[str, Scenario] = _build_catalog()
SCENARIO_IDS: list[str] = list(SCENARIOS)

# Scenarios with a prior quarter — the only ones a REAL investigation can
# compare against (Q1 is the initial period with no prior).
REAL_ELIGIBLE_IDS: list[str] = [sid for sid, s in SCENARIOS.items() if s.prior_quarter is not None]
