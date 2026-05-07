"""Unit tests for ``agentic_audit.layer2_narrative.sweep``.

Pure function — no mocks, no fixtures, just deterministic
combination enumeration. The shape contract this enforces is
load-bearing: the sweep driver, the gold-table key, and the
task_08 baseline denominator (32 narratives) all assume these
exact tuples.
"""

from __future__ import annotations

from agentic_audit.layer2_narrative.sweep import iter_narratable_combinations
from agentic_audit.models.evidence import LAYER3_ATTRIBUTES_PER_CONTROL


def test_iter_returns_exactly_32_combinations() -> None:
    """The headline contract — 32 narratable combinations per
    engagement. If this number drifts, task_08's `XX/32` baseline
    denominator silently changes, which would make any
    pass-rate comparison across runs invalid."""
    combos = list(iter_narratable_combinations(engagement_id="alpha"))
    assert len(combos) == 32


def test_iter_dc2_subset_is_12_combinations() -> None:
    """DC-2 has 3 narratable attributes (A, C, D — B is Layer 3)
    × 4 quarters = 12."""
    combos = [c for c in iter_narratable_combinations(engagement_id="alpha") if c[1] == "DC-2"]
    assert len(combos) == 12


def test_iter_dc9_subset_is_20_combinations() -> None:
    """DC-9 has 5 narratable attributes (A, B, C, E, F — D is
    Layer 3) × 4 quarters = 20."""
    combos = [c for c in iter_narratable_combinations(engagement_id="alpha") if c[1] == "DC-9"]
    assert len(combos) == 20


def test_iter_excludes_layer3_attributes() -> None:
    """DC-2.B and DC-9.D are reserved for Layer 3 React-loop and
    must NEVER appear in the Layer 2 sweep. If they did, the
    runtime guard inside ``NarrativeGenerator.generate()`` would
    raise — but we want the iterator to be the first line of
    defence, not the generator."""
    layer3_pairs = {
        (control, attribute)
        for control, attrs in LAYER3_ATTRIBUTES_PER_CONTROL.items()
        for attribute in attrs
    }
    for _eng, control, _quarter, attribute in iter_narratable_combinations(engagement_id="alpha"):
        assert (control, attribute) not in layer3_pairs


def test_iter_uses_engagement_id_arg() -> None:
    """The ``engagement_id`` parameter propagates verbatim into
    every tuple. Multi-engagement sweeps (future) will rely on
    this."""
    combos = list(iter_narratable_combinations(engagement_id="bravo-2026"))
    assert all(c[0] == "bravo-2026" for c in combos)


def test_iter_yields_tuples_in_stable_order() -> None:
    """Order is (control DC-2 → DC-9), then (quarter Q1 → Q4),
    then (attribute alphabetical). Stable for log-grep and
    deterministic gold-table population."""
    combos = list(iter_narratable_combinations(engagement_id="alpha"))
    # First combo must be DC-2 / Q1 / A (the alphabetically and
    # quarter-wise first narratable triple).
    assert combos[0] == ("alpha", "DC-2", "Q1", "A")
    # Last combo must be DC-9 / Q4 / F (last narratable in last
    # quarter of last control).
    assert combos[-1] == ("alpha", "DC-9", "Q4", "F")


def test_iter_combinations_are_unique() -> None:
    """No duplicate tuples — the gold-table composite key would
    otherwise collide on a same-prompt-version sweep."""
    combos = list(iter_narratable_combinations(engagement_id="alpha"))
    assert len(set(combos)) == len(combos)
