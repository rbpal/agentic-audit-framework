"""Tests for the per-control narratable / Layer-3 attribute constants.

These constants encode the asymmetric reservation derived in Decision 2
of ``privateDocs/step_05_layer2_narrative.md``:

- DC-2 narratable: A, C, D — B reserved for Layer 3 (variance
  explanation plausibility).
- DC-9 narratable: A, B, C, E, F — D reserved for Layer 3 (rate-change
  amendment plausibility).

The tests below pin the exact contents so that any future re-litigation
of the reservation surfaces here as a failing test, with the doc
reference in the assertion message pointing at where to settle the
change.
"""

from __future__ import annotations

from agentic_audit.models.evidence import (
    ATTRIBUTE_DEFINITIONS_PER_CONTROL,
    ATTRIBUTES_PER_CONTROL,
    LAYER3_ATTRIBUTES_PER_CONTROL,
    NARRATABLE_ATTRIBUTES_PER_CONTROL,
)

# ---------- pinned contents ------------------------------------------------


def test_narratable_per_control_pinned_to_decision_2() -> None:
    assert NARRATABLE_ATTRIBUTES_PER_CONTROL == {
        "DC-2": ["A", "C", "D"],
        "DC-9": ["A", "B", "C", "E", "F"],
    }


def test_layer3_per_control_pinned_to_decision_2() -> None:
    assert LAYER3_ATTRIBUTES_PER_CONTROL == {
        "DC-2": ["B"],
        "DC-9": ["D"],
    }


# ---------- partition invariants ------------------------------------------


def test_narratable_and_layer3_partition_attributes_per_control() -> None:
    """For every control, narratable ∪ layer3 == ATTRIBUTES_PER_CONTROL,
    and the two sets are disjoint. No attribute is double-counted; no
    attribute falls between the cracks.
    """
    for control_id, all_attrs in ATTRIBUTES_PER_CONTROL.items():
        narratable = set(NARRATABLE_ATTRIBUTES_PER_CONTROL[control_id])
        layer3 = set(LAYER3_ATTRIBUTES_PER_CONTROL[control_id])
        assert narratable.isdisjoint(layer3), (
            f"{control_id}: attribute(s) {narratable & layer3} appear in both "
            f"narratable and layer3 — see Decision 2 in step_05_layer2_narrative.md"
        )
        assert narratable | layer3 == set(all_attrs), (
            f"{control_id}: narratable ∪ layer3 = {narratable | layer3}, "
            f"but ATTRIBUTES_PER_CONTROL has {set(all_attrs)} — partition broken"
        )


def test_constants_share_control_id_keys() -> None:
    assert set(NARRATABLE_ATTRIBUTES_PER_CONTROL) == set(ATTRIBUTES_PER_CONTROL)
    assert set(LAYER3_ATTRIBUTES_PER_CONTROL) == set(ATTRIBUTES_PER_CONTROL)


# ---------- arithmetic invariants -----------------------------------------


def test_total_narratable_count_equals_32_for_full_sweep() -> None:
    """8 scenarios × narratable-attrs-per-quarter = 32 narratives.

    Pinned to Decision 1 in step_05_layer2_narrative.md. If this number
    moves, the full-sweep driver and hallucination-baseline denominator
    move with it — the failure here is the breadcrumb.
    """
    quarters = 4
    dc2_count = len(NARRATABLE_ATTRIBUTES_PER_CONTROL["DC-2"]) * quarters
    dc9_count = len(NARRATABLE_ATTRIBUTES_PER_CONTROL["DC-9"]) * quarters
    assert dc2_count == 12
    assert dc9_count == 20
    assert dc2_count + dc9_count == 32


# ---------- ATTRIBUTE_DEFINITIONS_PER_CONTROL (Step 6 task_04 prep) -------
#
# Lifted from generator/engagement_writers/toc.py:32-46 (the canonical
# strings written into the engagement TOC sheet) into a public model
# constant. The Step 6 judge reads `ATTRIBUTE_DEFINITIONS_PER_CONTROL[
# control][attribute]` to build its `attribute_definition` prompt
# placeholder, so the same canonical text drives both the corpus
# generation AND the judge. Single source of truth — see hand-off in
# privateDocs/step_06_eval_harness.md task_03.


def test_attribute_definitions_pinned_to_toc_canonical_strings() -> None:
    """Verbatim from toc.py's _DC2_ATTRIBUTES / _DC9_ATTRIBUTES tuples.

    Any change to these strings is a corpus-vs-judge contract change
    that must update both the engagement TOC writer AND the judge
    prompt invocation in lockstep. Pinning here makes that linkage
    enforceable.
    """
    assert ATTRIBUTE_DEFINITIONS_PER_CONTROL == {
        "DC-2": {
            "A": "Current-period accrual data loaded completely",
            "B": "Variances above threshold have recorded explanation",
            "C": "Explanations are consistent with upstream source",
            "D": "Reviewer signed off on the variance analysis",
        },
        "DC-9": {
            "A": "Preparer signed off on the Checklist",
            "B": "Independent reviewer signed off",
            "C": "Billing formulas tie to underlying supporting schedule",
            "D": "Billing rate change supported by governing-document amendment",
            "E": "Asset additions and retirements on the supporting schedule",
            "F": "Ownership-share percentages match supporting reference file",
        },
    }


def test_attribute_definitions_cover_every_attribute() -> None:
    """For every control, every attribute in ATTRIBUTES_PER_CONTROL has
    a non-empty definition. No gaps, no extras."""
    for control_id, attributes in ATTRIBUTES_PER_CONTROL.items():
        defs = ATTRIBUTE_DEFINITIONS_PER_CONTROL[control_id]
        assert set(defs) == set(attributes), (
            f"{control_id}: ATTRIBUTE_DEFINITIONS keys {set(defs)} "
            f"!= ATTRIBUTES_PER_CONTROL {set(attributes)}"
        )
        for attr, definition in defs.items():
            assert definition.strip(), (
                f"{control_id}.{attr}: definition is empty or whitespace-only"
            )
