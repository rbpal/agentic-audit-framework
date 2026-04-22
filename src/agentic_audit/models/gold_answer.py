"""Typed gold-answer JSON schema for scenario evaluation.

One ``GoldAnswer`` per scenario. The audit pipeline NEVER reads these
files — they are the teacher's answer key for the evaluation harness
(Step 12). Kept in sync with Task 3's tickmark placement via
``scenario.pick_exception_attribute`` — that function is the single
source of truth for "which attribute fails on an exception scenario,"
and lives in models/ so both the generator and this module can consume
it without a circular import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentic_audit.models.scenario import (
    ControlId,
    ExpectedOutcome,
    ScenarioSpec,
    pick_exception_attribute,
)

AttributeResult = Literal["pass", "fail"]
FinalVerdict = Literal["Effective", "Not effective"]


# Attribute counts per control — mirrors fake_data / workbook_writer dispatch.
_CONTROL_ATTRIBUTE_COUNT: dict[ControlId, int] = {
    "DC-9": 6,
    "DC-2": 4,
}

# Narrative keywords per exception_type. The audit agent's Layer-2 output
# must contain at least one of these substrings (case-insensitive) for
# an eval-correct result. Keep keywords conservative — common words that
# a reasonable narrative would naturally produce.
_EXCEPTION_NARRATIVE_KEYWORDS: dict[str, list[str]] = {
    "signoff_missing": ["signoff", "absent", "missing", "reviewer"],
    "figure_mismatch": ["mismatch", "backing", "tie"],
    "billing_rate_change_with_amendment": ["amendment", "rate", "change"],
    "billing_rate_change_without_amendment": ["rate", "change", "amendment"],
    "variance_above_threshold_no_explanation": [
        "variance",
        "threshold",
        "explanation",
    ],
    "variance_explanation_inadequate": ["variance", "inadequate", "insufficient"],
    "boundary_edge_case": ["boundary", "period", "cutoff"],
}


class GoldAnswer(BaseModel):
    """Teacher's answer key for one scenario.

    Every synthetic workbook (.xlsx) has a paired gold-answer (.json).
    The audit pipeline processes the .xlsx and produces its own answer;
    evaluation compares the pipeline's output to this GoldAnswer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str = Field(..., min_length=5, max_length=80)
    control_id: ControlId
    expected_outcome: ExpectedOutcome
    # Keyed by ``"{control_id}.{attribute_letter}"``, e.g. ``"DC-9.D"``.
    expected_per_attribute_result: dict[str, AttributeResult]
    expected_final_verdict: FinalVerdict
    expected_exception_narrative_keywords: list[str] = Field(default_factory=list)


def build_gold_answer(spec: ScenarioSpec) -> GoldAnswer:
    """Derive the gold answer deterministically from a ScenarioSpec.

    Reuses ``fake_data.pick_exception_attribute`` — the SAME function
    Task 3's tickmark-placement uses — so the gold JSON's fail-flag
    is guaranteed to match the position of the "X" tickmark in the
    rendered workbook.
    """
    n_attrs = _CONTROL_ATTRIBUTE_COUNT[spec.control_id]
    letters = "ABCDEF"[:n_attrs]
    failing_letter = pick_exception_attribute(spec)  # "" if pass, else "A"-"F"

    per_attr: dict[str, AttributeResult] = {}
    for letter in letters:
        key = f"{spec.control_id}.{letter}"
        per_attr[key] = "fail" if letter == failing_letter else "pass"

    if spec.expected_outcome == "pass":
        narrative_keywords: list[str] = []
    else:
        narrative_keywords = list(_EXCEPTION_NARRATIVE_KEYWORDS.get(spec.exception_type, []))

    return GoldAnswer(
        scenario_id=spec.scenario_id,
        control_id=spec.control_id,
        expected_outcome=spec.expected_outcome,
        expected_per_attribute_result=per_attr,
        expected_final_verdict=(
            "Effective" if spec.expected_outcome == "pass" else "Not effective"
        ),
        expected_exception_narrative_keywords=narrative_keywords,
    )


def load_gold_answer(path: Path) -> GoldAnswer:
    """Load + validate a gold-answer JSON file."""
    return GoldAnswer.model_validate_json(path.read_text())


def gold_answer_to_json(answer: GoldAnswer) -> str:
    """Serialize a GoldAnswer to JSON with stable key ordering."""
    return json.dumps(answer.model_dump(mode="json"), indent=2, sort_keys=True)
