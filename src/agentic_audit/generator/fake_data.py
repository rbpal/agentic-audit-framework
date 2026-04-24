"""Seeded fake-data generators for the synthetic audit corpus.

Every provider takes a local ``random.Random`` instance — never touches
the global random state. Values drawn from constrained pools per the
§4.5 conventions in privateDocs/step_01_synthetic_data.md:

* USD currency (no GBP)
* Greek-letter fund names (no fruits, no real entities)
* Fabricated 2-letter initials (no real auditor names)

NOTE ON CONTROL ATTRIBUTES AND DESCRIPTIONS: the attribute descriptions
and testing procedures below reflect **generic SOX audit patterns**
(PCAOB AS 2201 / COSO internal-control framework). They are not
derived from, and do not reproduce, any specific prior-engagement
attribute wording. Industry-neutral phrasing is used throughout
("governing-document amendment", "supporting reference file",
"supporting schedule") rather than sector-specific terminology.
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Literal

from agentic_audit.models.scenario import (
    ExceptionType,
    ExpectedOutcome,
    PatternType,
    Quarter,
    ScenarioSpec,
    pick_exception_attribute,
)

# ── Constrained pools (fabricated — no real personnel / entities) ─────

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
    "TA",
    "UE",
    "VI",
    "WD",
)

_GREEK_FUND_NAMES: tuple[str, ...] = (
    "Alpha Pension Fund",
    "Beta International Fund",
    "Gamma Sub-Fund",
    "Delta Sub-Fund",
    "Epsilon Holdings",
    "Zeta Residual Fund",
    "Eta Growth Mandate",
    "Theta Credit Fund",
)

_CONTROL_TYPES: tuple[str, ...] = (
    "IT dependent manual control",
    "Manual control",
    "IT automated control",
)

_GAAS_LABELS: tuple[str, ...] = ("US GAAS", "ISA (UK)", "PCAOB AS")

_ENGAGEMENT_NATURES: tuple[str, ...] = (
    "Integrated audit of financial statements and internal control",
    "Financial statement audit under PCAOB standards",
    "Annual audit of investment management entity",
)

_SCOT_NAMES: tuple[str, ...] = ("Revenue", "Billing", "Fee Calculation")

_IT_APPLICATIONS: tuple[str, ...] = (
    "Paxus, Excel",
    "Charles River, GL",
    "Fee Calc Platform, GL",
)

# Exception-attribute mapping moved to models.scenario (Task 5 refactor):
# single source of truth shared by this module (tickmark placement) and
# gold_answer.build_gold_answer (JSON emission). Import the helper
# ``pick_exception_attribute`` instead of re-declaring the map here.


# ── Simple providers (stateless, rng-only) ────────────────────────────


def fake_workpaper_ref(rng: random.Random) -> str:
    """Dotted ref code in the form ``N.NN`` (e.g. ``4.06``)."""
    return f"{rng.randint(1, 9)}.{rng.randint(1, 9):02}"


def fake_entity_name(rng: random.Random) -> str:
    """2–3 word entity name drawn from the Greek-letter pool."""
    return rng.choice(_GREEK_FUND_NAMES)


def fake_initials(rng: random.Random) -> str:
    """2-letter uppercase initials from the fabricated pool."""
    return rng.choice(_INITIALS_POOL)


def fake_yes_no(rng: random.Random) -> str:
    return rng.choice(("Yes", "No"))


def fake_gaas(rng: random.Random) -> str:
    return rng.choice(_GAAS_LABELS)


def fake_engagement_nature(rng: random.Random) -> str:
    return rng.choice(_ENGAGEMENT_NATURES)


def fake_scot_name(rng: random.Random) -> str:
    return rng.choice(_SCOT_NAMES)


def fake_control_type(rng: random.Random) -> str:
    return rng.choice(_CONTROL_TYPES)


def fake_it_applications(rng: random.Random) -> str:
    return rng.choice(_IT_APPLICATIONS)


def fake_year_end_date(rng: random.Random, quarter: Quarter) -> dt.date:
    """Date inside the declared quarter (2025 base year)."""
    ranges: dict[Quarter, tuple[dt.date, dt.date]] = {
        "Q1": (dt.date(2025, 1, 1), dt.date(2025, 3, 31)),
        "Q3": (dt.date(2025, 7, 1), dt.date(2025, 9, 30)),
    }
    start, end = ranges[quarter]
    span_days = (end - start).days
    return start + dt.timedelta(days=rng.randint(0, span_days))


def fake_asset_value_usd(rng: random.Random) -> int:
    """USD asset value in the $10M–$500M range (plausible mid-tier fund)."""
    return rng.randint(10_000_000, 500_000_000)


def fake_billing_rate_usd(rng: random.Random) -> int:
    """Quarterly billing rate in the $50K–$500K range."""
    return rng.randint(50_000, 500_000)


# ── Canonical billing computation (Task 13) ──────────────────────────
#
# Shared between the billing_calc W/P writer and the TOC populator so
# both see the same "actual" numbers. The TOC's *claimed* figure may
# intentionally differ from the W/P's actual figure — see
# ``compute_toc_billing_claim`` — that's how ``figure_mismatch``
# scenarios encode a cross-file disagreement the agent must detect.

_BILLING_RATES: tuple[str, ...] = ("0.25%", "0.50%", "0.75%", "1.00%")


def compute_canonical_billing(spec: ScenarioSpec) -> tuple[int, str, int]:
    """Return ``(asset_value, rate_str, billing_fee)`` for this scenario.

    Deterministic per ``spec.seed`` — same scenario always produces the
    same triple. Billing calc writer reads this as its source of truth.
    TOC populator reads this too (see ``compute_toc_billing_claim``).

    The rng is namespaced with a fixed string so that adding or removing
    billing fields doesn't perturb the TOC's unrelated rng draws.
    """
    rng = random.Random(f"{spec.seed}_billing")
    asset_value = rng.randint(10_000_000, 500_000_000)
    rate_str = rng.choice(_BILLING_RATES)
    rate_decimal = float(rate_str.rstrip("%")) / 100.0
    billing_fee = int(asset_value * rate_decimal)
    return asset_value, rate_str, billing_fee


def compute_toc_billing_claim(spec: ScenarioSpec) -> int:
    """The billing fee the TOC *asserts* — may or may not match the W/P.

    * pass and most exceptions → matches the billing calc fee exactly
      (auditor correctly recorded what the supporting schedule showed).
    * ``figure_mismatch`` → intentionally offset by +5 % (rounded to the
      nearest thousand) so the TOC disagrees with the billing calc.
      That disagreement is the contradiction the agent must detect.
    """
    _, _, canonical_fee = compute_canonical_billing(spec)
    if spec.exception_type == "figure_mismatch":
        offset = max(1_000, round(canonical_fee * 0.05 / 1_000) * 1_000)
        return canonical_fee + offset
    return canonical_fee


# ── Outcome-aware providers ───────────────────────────────────────────


def effectiveness_conclusion(outcome: ExpectedOutcome) -> str:
    """Deterministic from outcome — no RNG dependence."""
    return "Effective" if outcome == "pass" else "Not effective"


def exceptions_noted(outcome: ExpectedOutcome) -> str:
    """Deterministic from outcome."""
    return "No" if outcome == "pass" else "Yes"


def fake_tickmark(
    rng: random.Random,
    spec: ScenarioSpec,
    attribute_letter: str,
) -> Literal["a", "X"]:
    """Tickmark value for one (sample, attribute) cell.

    Pass scenarios → always ``"a"``.
    Exception scenarios → ``"X"`` if this attribute is the one that fails,
    else ``"a"``. Same mapping across all sample rows for consistency.
    """
    del rng  # deterministic from spec alone
    if spec.expected_outcome == "pass":
        return "a"
    failing = pick_exception_attribute(spec)
    return "X" if attribute_letter == failing else "a"


# ── Pattern-aware free-text providers ────────────────────────────────


_CONTROL_DESCRIPTIONS: dict[PatternType, tuple[str, ...]] = {
    "signoff_with_tieout": (
        "Quarterly review of the billing calculation with six-attribute "
        "sign-off covering formula accuracy, cross-tab tie-out, billing-rate "
        "continuity, asset additions/retirements, and ownership-share rates.",
        "The fee-calculation reviewer performs a six-attribute check and "
        "signs off that the billing summary ties through to the underlying "
        "supporting schedules each quarter.",
    ),
    "variance_detection": (
        "Monthly variance analysis of each revenue stream; any period-over-"
        "period movement above a defined monetary threshold must have a "
        "recorded explanation reviewed by the finance lead.",
        "The finance team compares current-period accruals to prior-period "
        "accruals by revenue stream; variances above threshold require "
        "written explanation reconciled to upstream source data.",
    ),
}


def fake_control_description(rng: random.Random, spec: ScenarioSpec) -> str:
    return rng.choice(_CONTROL_DESCRIPTIONS[spec.pattern_type])


_POPULATION_DESCRIPTIONS: dict[PatternType, tuple[str, ...]] = {
    "signoff_with_tieout": (
        "Population consists of each quarter's completed billing-calculation "
        "workpaper. We selected two of the four quarters for detailed testing.",
    ),
    "variance_detection": (
        "Population consists of each month's variance-analysis spreadsheet "
        "prepared by the finance team. We selected two months for detailed "
        "testing.",
    ),
}


def fake_population_description(rng: random.Random, spec: ScenarioSpec) -> str:
    return rng.choice(_POPULATION_DESCRIPTIONS[spec.pattern_type])


_ATTRIBUTE_DESCRIPTIONS: dict[PatternType, dict[str, str]] = {
    "signoff_with_tieout": {
        "A": "Preparer signed off with date on the Checklist.",
        "B": "Independent reviewer signed off with date.",
        "C": "Billing formulas tie to the underlying supporting schedules.",
        "D": "Billing rate change is supported by a governing-document amendment.",
        "E": "Asset additions and retirements appear on the supporting schedule.",
        "F": "Ownership-share percentages match the supporting reference file.",
    },
    "variance_detection": {
        "A": "Current-period accrual data is loaded completely and accurately.",
        "B": "Variances above threshold have a recorded explanation.",
        "C": "Explanations are consistent with upstream source data.",
        "D": "Reviewer signed off on the completed variance-analysis spreadsheet.",
    },
}


def fake_attribute_description(spec: ScenarioSpec, letter: str) -> str:
    """Return the canonical description for (pattern, attribute letter).

    Deterministic — no RNG — so gold JSON can re-derive without a seed.
    """
    return _ATTRIBUTE_DESCRIPTIONS[spec.pattern_type][letter]


_TOC_PROCEDURES: dict[PatternType, dict[str, str]] = {
    "signoff_with_tieout": {
        "A": "Inspect Checklist column F for preparer initials and date.",
        "B": "Inspect Checklist column G for reviewer initials and date.",
        "C": "Recompute the billing total from the supporting schedule and tie to the summary.",
        "D": "Inspect the governing-document file for an amendment matching any rate change.",
        "E": "Compare asset additions and retirements listed on the supporting schedule.",
        "F": "Recompute ownership-share percentages and agree to the supporting reference file.",
    },
    "variance_detection": {
        "A": "Agree current-period load totals to the upstream data feed.",
        "B": "For each variance above threshold, inspect the explanation column.",
        "C": "For each explanation, agree the referenced figure to the upstream source.",
        "D": "Inspect the reviewer sign-off line on the variance-analysis workbook.",
    },
}


def fake_toc_procedure(spec: ScenarioSpec, letter: str) -> str:
    return _TOC_PROCEDURES[spec.pattern_type][letter]


def fake_sample_description(rng: random.Random, spec: ScenarioSpec, index: int) -> str:
    del rng
    del index
    if spec.pattern_type == "signoff_with_tieout":
        return "Quarterly billing-calculation workpaper"
    return "Monthly variance-analysis workbook"


def fake_sample_period(rng: random.Random, spec: ScenarioSpec, index: int) -> str:
    """Period string for the sample grid.

    Quarterly → ``"Q<N> <year>"``; Monthly → ``"<Month> <year>"``.
    ``index`` is 1-based; samples walk backward within the declared quarter.
    """
    del rng
    year = 2025
    if spec.pattern_type == "signoff_with_tieout":
        # Two quarters; index 1 = the declared quarter, index 2 = prior quarter
        quarter_num = int(spec.quarter[1])
        if index == 1:
            return f"Q{quarter_num} {year}"
        prior = quarter_num - 1 if quarter_num > 1 else 4
        return f"Q{prior} {year if quarter_num > 1 else year - 1}"
    # Monthly — pick two months within the declared quarter
    months = ("January", "February") if spec.quarter == "Q1" else ("July", "August")
    return f"{months[index - 1]} {year}"


def fake_wp_ref(rng: random.Random) -> str:
    """Cross-workpaper reference in the ``DC-N.M`` form."""
    return f"DC-{rng.randint(1, 17)}.{rng.randint(1, 9)}"


def fake_exception_narrative(spec: ScenarioSpec) -> str:
    """Short explanation consistent with spec.exception_type."""
    if spec.expected_outcome == "pass":
        return "N/A"
    narratives: dict[ExceptionType, str] = {
        "none": "N/A",
        "signoff_missing": "Reviewer sign-off absent on one sample item.",
        "figure_mismatch": "Backing schedule total does not tie to summary figure.",
        "billing_rate_change_with_amendment": (
            "Rate change present; governing-document amendment located."
        ),
        "billing_rate_change_without_amendment": (
            "Rate change present without corresponding governing-document amendment."
        ),
        "variance_above_threshold_no_explanation": (
            "Variance above threshold with no recorded explanation."
        ),
        "variance_explanation_inadequate": (
            "Variance explanation insufficient to reconcile the movement."
        ),
        "boundary_edge_case": "Quarter-boundary transaction with ambiguous period assignment.",
    }
    return narratives[spec.exception_type]
