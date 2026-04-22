"""Unit tests for fake_data providers."""

from __future__ import annotations

import datetime as dt
import random
import re

import pytest

from agentic_audit.generator import fake_data
from agentic_audit.models.scenario import ControlId, PatternType, Quarter, ScenarioSpec


def _make_spec(
    control_id: ControlId = "DC-9",
    pattern_type: PatternType = "signoff_with_tieout",
    quarter: Quarter = "Q1",
    expected_outcome: str = "pass",
    exception_type: str = "none",
    seed: int = 42,
) -> ScenarioSpec:
    return ScenarioSpec(
        scenario_id=f"x_test_spec_{seed}",
        control_id=control_id,
        pattern_type=pattern_type,
        quarter=quarter,
        expected_outcome=expected_outcome,  # type: ignore[arg-type]
        exception_type=exception_type,  # type: ignore[arg-type]
        seed=seed,
    )


# ── Format / pattern tests ────────────────────────────────────────────


def test_fake_workpaper_ref_matches_pattern() -> None:
    rng = random.Random(42)
    for _ in range(10):
        ref = fake_data.fake_workpaper_ref(rng)
        assert re.match(r"^\d\.\d{2}$", ref), f"{ref!r} does not match N.NN"


def test_fake_initials_from_pool() -> None:
    rng = random.Random(42)
    for _ in range(20):
        ini = fake_data.fake_initials(rng)
        assert ini in fake_data._INITIALS_POOL


def test_fake_entity_name_from_greek_pool() -> None:
    rng = random.Random(42)
    name = fake_data.fake_entity_name(rng)
    assert name in fake_data._GREEK_FUND_NAMES


def test_fake_yes_no_in_binary_set() -> None:
    rng = random.Random(42)
    for _ in range(10):
        assert fake_data.fake_yes_no(rng) in {"Yes", "No"}


# ── Range / quarter tests ─────────────────────────────────────────────


def test_fake_year_end_date_q1_in_range() -> None:
    rng = random.Random(42)
    for _ in range(20):
        d = fake_data.fake_year_end_date(rng, "Q1")
        assert dt.date(2025, 1, 1) <= d <= dt.date(2025, 3, 31)


def test_fake_year_end_date_q3_in_range() -> None:
    rng = random.Random(42)
    for _ in range(20):
        d = fake_data.fake_year_end_date(rng, "Q3")
        assert dt.date(2025, 7, 1) <= d <= dt.date(2025, 9, 30)


def test_fake_asset_value_usd_in_range() -> None:
    rng = random.Random(42)
    for _ in range(20):
        v = fake_data.fake_asset_value_usd(rng)
        assert 10_000_000 <= v <= 500_000_000


def test_fake_billing_rate_usd_in_range() -> None:
    rng = random.Random(42)
    for _ in range(20):
        v = fake_data.fake_billing_rate_usd(rng)
        assert 50_000 <= v <= 500_000


# ── Determinism ────────────────────────────────────────────────────────


def test_same_seed_same_output() -> None:
    rng1, rng2 = random.Random(42), random.Random(42)
    assert fake_data.fake_entity_name(rng1) == fake_data.fake_entity_name(rng2)
    assert fake_data.fake_workpaper_ref(rng1) == fake_data.fake_workpaper_ref(rng2)
    assert fake_data.fake_initials(rng1) == fake_data.fake_initials(rng2)


def test_different_seed_eventually_differs() -> None:
    rng1, rng2 = random.Random(42), random.Random(43)
    # Drawing many values — at least some must differ across seeds
    outputs1 = [fake_data.fake_workpaper_ref(rng1) for _ in range(20)]
    outputs2 = [fake_data.fake_workpaper_ref(rng2) for _ in range(20)]
    assert outputs1 != outputs2


# ── Outcome-aware deterministic providers ─────────────────────────────


def test_effectiveness_conclusion_pass() -> None:
    assert fake_data.effectiveness_conclusion("pass") == "Effective"


def test_effectiveness_conclusion_exception() -> None:
    assert fake_data.effectiveness_conclusion("exception") == "Not effective"


def test_exceptions_noted_pass() -> None:
    assert fake_data.exceptions_noted("pass") == "No"


def test_exceptions_noted_exception() -> None:
    assert fake_data.exceptions_noted("exception") == "Yes"


# ── Tickmark logic ────────────────────────────────────────────────────


def test_tickmark_pass_always_a() -> None:
    rng = random.Random(0)
    spec = _make_spec(expected_outcome="pass")
    for letter in "ABCDEF":
        assert fake_data.fake_tickmark(rng, spec, letter) == "a"


def test_tickmark_exception_flags_failing_attribute() -> None:
    rng = random.Random(0)
    spec = _make_spec(
        control_id="DC-9",
        pattern_type="signoff_with_tieout",
        expected_outcome="exception",
        exception_type="figure_mismatch",
    )
    # figure_mismatch maps to attribute D
    assert fake_data.fake_tickmark(rng, spec, "D") == "X"
    assert fake_data.fake_tickmark(rng, spec, "A") == "a"
    assert fake_data.fake_tickmark(rng, spec, "F") == "a"


def test_pick_exception_attribute_pass_returns_empty() -> None:
    spec = _make_spec(expected_outcome="pass")
    assert fake_data.pick_exception_attribute(spec) == ""


def test_pick_exception_attribute_signoff_missing_is_b() -> None:
    spec = _make_spec(expected_outcome="exception", exception_type="signoff_missing")
    assert fake_data.pick_exception_attribute(spec) == "B"


# ── Attribute / ToC-procedure registries ─────────────────────────────


def test_dc9_attribute_descriptions_has_six() -> None:
    spec = _make_spec(control_id="DC-9", pattern_type="signoff_with_tieout")
    for letter in "ABCDEF":
        desc = fake_data.fake_attribute_description(spec, letter)
        assert desc and isinstance(desc, str)


def test_dc2_attribute_descriptions_has_four() -> None:
    spec = _make_spec(control_id="DC-2", pattern_type="variance_detection")
    for letter in "ABCD":
        desc = fake_data.fake_attribute_description(spec, letter)
        assert desc and isinstance(desc, str)


def test_fake_sample_period_q1_monthly_returns_january_february() -> None:
    spec = _make_spec(control_id="DC-2", pattern_type="variance_detection", quarter="Q1")
    p1 = fake_data.fake_sample_period(random.Random(0), spec, 1)
    p2 = fake_data.fake_sample_period(random.Random(0), spec, 2)
    assert p1 == "January 2025"
    assert p2 == "February 2025"


def test_fake_sample_period_q1_quarterly_returns_quarter_strings() -> None:
    spec = _make_spec(control_id="DC-9", pattern_type="signoff_with_tieout", quarter="Q1")
    p1 = fake_data.fake_sample_period(random.Random(0), spec, 1)
    p2 = fake_data.fake_sample_period(random.Random(0), spec, 2)
    assert p1 == "Q1 2025"
    # Prior quarter wraps to Q4 of previous year
    assert p2 == "Q4 2024"


def test_fake_wp_ref_matches_pattern() -> None:
    rng = random.Random(42)
    ref = fake_data.fake_wp_ref(rng)
    assert re.match(r"^DC-\d+\.\d+$", ref)


# ── Exception narrative ──────────────────────────────────────────────


def test_exception_narrative_pass_is_na() -> None:
    spec = _make_spec(expected_outcome="pass")
    assert fake_data.fake_exception_narrative(spec) == "N/A"


def test_exception_narrative_variance_mentions_threshold() -> None:
    spec = _make_spec(
        control_id="DC-2",
        pattern_type="variance_detection",
        expected_outcome="exception",
        exception_type="variance_above_threshold_no_explanation",
    )
    text = fake_data.fake_exception_narrative(spec)
    assert "threshold" in text.lower() or "explanation" in text.lower()


# ── No global random pollution ────────────────────────────────────────


def test_providers_do_not_touch_global_random() -> None:
    random.seed(999)
    before = [random.random() for _ in range(5)]
    random.seed(999)
    rng = random.Random(42)
    _ = fake_data.fake_workpaper_ref(rng)
    _ = fake_data.fake_entity_name(rng)
    _ = fake_data.fake_initials(rng)
    after = [random.random() for _ in range(5)]
    assert before == after


@pytest.mark.parametrize("currency_symbol", ["£", "GBP", "€", "EUR"])
def test_no_gbp_or_eur_in_control_descriptions(currency_symbol: str) -> None:
    for pattern in ("signoff_with_tieout", "variance_detection"):
        for desc in fake_data._CONTROL_DESCRIPTIONS[pattern]:  # type: ignore[index]
            assert currency_symbol not in desc
