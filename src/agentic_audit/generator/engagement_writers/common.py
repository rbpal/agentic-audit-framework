"""Shared helpers across v2 engagement writers.

Kept deliberately small — rng namespacing, per-quarter billing rate
progression, canonical numeric values. Anything needed by exactly one
writer stays in that writer's module.
"""

from __future__ import annotations

import datetime as dt
import random

from agentic_audit.models.engagement import EngagementSpec, Quarter

# ── Quarter-dated boundaries (2025 calendar) ─────────────────────────

_QUARTER_RANGES: dict[Quarter, tuple[dt.date, dt.date]] = {
    "Q1": (dt.date(2025, 1, 1), dt.date(2025, 3, 31)),
    "Q2": (dt.date(2025, 4, 1), dt.date(2025, 6, 30)),
    "Q3": (dt.date(2025, 7, 1), dt.date(2025, 9, 30)),
    "Q4": (dt.date(2025, 10, 1), dt.date(2025, 12, 31)),
}


def quarter_end_date(quarter: Quarter) -> dt.date:
    """Last calendar day of the quarter — used as the as-of date."""
    return _QUARTER_RANGES[quarter][1]


def date_in_quarter(rng: random.Random, quarter: Quarter) -> dt.date:
    """A random date inside the quarter — seeded, deterministic."""
    start, end = _QUARTER_RANGES[quarter]
    span_days = (end - start).days
    return start + dt.timedelta(days=rng.randint(0, span_days))


# ── Seeded rng helpers ───────────────────────────────────────────────


def seeded_rng(spec: EngagementSpec, namespace: str) -> random.Random:
    """Per-namespace rng derived from ``spec.seed``.

    Each writer picks its own namespace string — e.g.
    ``seeded_rng(spec, f"dc9_{quarter}_preparer")``. Two callers using the
    same namespace see the same rng stream; namespacing ensures adding a
    field to one writer doesn't perturb another writer's draws.
    """
    return random.Random(f"{spec.seed}_{namespace}")


# ── Identity pools — same as v1 (Greek funds, 2-letter initials) ────

_INITIALS_POOL: tuple[str, ...] = (
    "AK",
    "BN",
    "CR",
    "DM",
    "EP",
    "FV",
    "GT",
    "HW",
    "JS",
    "KL",
    "MO",
    "NQ",
    "PZ",
    "QX",
    "RU",
    "SY",
)


def pick_initials(rng: random.Random) -> str:
    return rng.choice(_INITIALS_POOL)


# ── DC-9 billing progression across quarters ────────────────────────
#
# Deterministic rate schedule per the §5 defect plan:
#
#   Q1: 0.25%  (baseline)
#   Q2: 0.50%  (rate change — benign, amendment on file)
#   Q3: 0.50%  (unchanged; figure_mismatch defect lives elsewhere)
#   Q4: 0.75%  (rate change — DEFECT: no amendment)
#
# The "prior period" for Q1 is "N/A — first period". For Q2-Q4 it is the
# preceding quarter's rate.

_BILLING_RATES: dict[Quarter, str] = {
    "Q1": "0.25%",
    "Q2": "0.50%",
    "Q3": "0.50%",
    "Q4": "0.75%",
}

_PRIOR_QUARTER: dict[Quarter, Quarter | None] = {
    "Q1": None,
    "Q2": "Q1",
    "Q3": "Q2",
    "Q4": "Q3",
}


def billing_rate(quarter: Quarter) -> str:
    """String form — e.g. ``"0.50%"`` — used directly in workbook cells."""
    return _BILLING_RATES[quarter]


def billing_rate_decimal(quarter: Quarter) -> float:
    """Numeric form of the quarter's billing rate (0.0025, 0.005, ...)."""
    return float(_BILLING_RATES[quarter].rstrip("%")) / 100.0


def prior_quarter(quarter: Quarter) -> Quarter | None:
    """``None`` for Q1; the preceding quarter otherwise."""
    return _PRIOR_QUARTER[quarter]


def rate_changed_this_quarter(quarter: Quarter) -> bool:
    """True if the rate this quarter differs from the prior quarter's."""
    prior = prior_quarter(quarter)
    if prior is None:
        return False
    return billing_rate(quarter) != billing_rate(prior)


def canonical_asset_value(spec: EngagementSpec, quarter: Quarter) -> int:
    """USD asset value for a given quarter — deterministic per engagement seed.

    Range: $100M–$500M. Same seed + quarter always returns the same value.
    """
    rng = seeded_rng(spec, f"dc9_asset_value_{quarter}")
    return rng.randint(100_000_000, 500_000_000)


def canonical_billing_fee(spec: EngagementSpec, quarter: Quarter) -> int:
    """Billing fee = asset_value × rate. The TOC's billing claim agrees
    with this for non-figure_mismatch quarters; disagrees for Q3 (defect).
    """
    return int(canonical_asset_value(spec, quarter) * billing_rate_decimal(quarter))
