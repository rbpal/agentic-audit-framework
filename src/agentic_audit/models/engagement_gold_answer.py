"""Per-(control × quarter) gold-answer schema for the v2 corpus.

Replaces v1's per-scenario ``GoldAnswer`` — v2 has 8 gold answers
(2 controls × 4 quarters). Each one captures:

* per-attribute pass/fail map — the agent's verdict must match this
* overall quarter verdict — Effective / Not effective
* optional cross-file contradiction pointer — names the TOC cell AND
  the W/P cell that disagree (for figure_mismatch, rate_change_without_
  amendment, variance_no_explanation, variance_explanation_inadequate,
  variance_boundary). The agent is scored on whether it identifies
  both cells and articulates the disagreement.

Gold answers are **never read** by the audit pipeline — they're the
evaluation harness's ground truth. Callers write them to disk via
``engagement_gold_answer_to_json`` and load with
``load_engagement_gold_answer``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict

from agentic_audit.models.engagement import (
    ControlId,
    EngagementSpec,
    Quarter,
    QuarterDefect,
    quarter_control,
)

AttributeResult: TypeAlias = Literal["pass", "fail"]
FinalVerdict: TypeAlias = Literal["Effective", "Not effective"]

# ── Defect → failing attribute letter (mirrors toc.py) ──────────────

_DEFECT_TO_ATTRIBUTE: dict[QuarterDefect, str | None] = {
    "none": None,
    "dc9_figure_mismatch": "C",
    "dc9_rate_change_with_amendment": None,  # benign
    "dc9_rate_change_without_amendment": "D",
    "dc2_variance_no_explanation": "B",
    "dc2_variance_explanation_inadequate": "C",
    "dc2_variance_boundary": "A",
}

# Cross-file contradiction templates — name the exact TOC cell + W/P
# cell the agent must cross-reference. `{quarter}` placeholder is
# formatted with the actual quarter at build time.
_CROSS_FILE: dict[QuarterDefect, dict[str, str] | None] = {
    "none": None,
    "dc9_rate_change_with_amendment": None,  # benign — no contradiction
    "dc9_figure_mismatch": {
        "description": ("TOC billing-fee claim does not tie to DC-9 W/P billing calc"),
        "toc_cell": ("engagement TOC, DC-9 sheet, row 12 (Billing fee per W/P), {quarter} column"),
        "wp_cell": "dc9_{quarter}_ref.xlsx, row 10 (Billing fee USD)",
    },
    "dc9_rate_change_without_amendment": {
        "description": (
            "W/P amendment row reads 'NO AMENDMENT FILED' — TOC attribute D tickmark is X"
        ),
        "toc_cell": ("engagement TOC, DC-9 sheet, row 8 (attribute D), {quarter} column"),
        "wp_cell": "dc9_{quarter}_ref.xlsx, row 16 (Supporting amendment)",
    },
    "dc2_variance_no_explanation": {
        "description": (
            "above-threshold variance row has blank explanation; TOC attribute B tickmark is X"
        ),
        "toc_cell": ("engagement TOC, DC-2 sheet, row 6 (attribute B), {quarter} column"),
        "wp_cell": ("dc2_{quarter}_ref.xlsx, variance-table rows 10-14, Explanation column (7)"),
    },
    "dc2_variance_explanation_inadequate": {
        "description": (
            "above-threshold variance rows have explanations but 'Source tie' = No; "
            "TOC attribute C tickmark is X"
        ),
        "toc_cell": ("engagement TOC, DC-2 sheet, row 7 (attribute C), {quarter} column"),
        "wp_cell": ("dc2_{quarter}_ref.xlsx, variance-table rows 10-14, Source tie column (8)"),
    },
    "dc2_variance_boundary": {
        "description": (
            "upstream feed total does not tie to workbook total; TOC attribute A tickmark is X"
        ),
        "toc_cell": ("engagement TOC, DC-2 sheet, row 5 (attribute A), {quarter} column"),
        "wp_cell": "dc2_{quarter}_ref.xlsx, row 7 (tie flag)",
    },
}

_CONTROL_ATTRIBUTE_COUNT: dict[ControlId, int] = {
    "DC-2": 4,
    "DC-9": 6,
}


class EngagementGoldAnswer(BaseModel):
    """Gold answer for one ``(control × quarter)`` pair in the v2 corpus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    control_id: ControlId
    quarter: Quarter
    defect: QuarterDefect
    expected_attribute_results: dict[str, AttributeResult]
    expected_quarter_verdict: FinalVerdict
    expected_cross_file_contradiction: dict[str, str] | None = None


# ── Builders ─────────────────────────────────────────────────────────


def build_quarter_gold_answer(
    spec: EngagementSpec,
    control_id: ControlId,
    quarter: Quarter,
) -> EngagementGoldAnswer:
    """Derive the gold answer for one ``(control, quarter)`` pair.

    Deterministic from the engagement's declared defect at that pair —
    no rng, no external state. Same ``(spec, control, quarter)`` always
    returns the same answer.
    """
    qc = quarter_control(spec, control_id, quarter)
    n_attrs = _CONTROL_ATTRIBUTE_COUNT[control_id]
    letters = "ABCDEF"[:n_attrs]
    failing_letter = _DEFECT_TO_ATTRIBUTE[qc.defect]

    per_attr: dict[str, AttributeResult] = {}
    for letter in letters:
        key = f"{control_id}.{letter}"
        per_attr[key] = "fail" if letter == failing_letter else "pass"

    verdict: FinalVerdict = "Not effective" if failing_letter is not None else "Effective"

    raw_contradiction = _CROSS_FILE.get(qc.defect)
    if raw_contradiction is not None:
        contradiction: dict[str, str] | None = {
            k: v.format(quarter=quarter) for k, v in raw_contradiction.items()
        }
    else:
        contradiction = None

    return EngagementGoldAnswer(
        control_id=control_id,
        quarter=quarter,
        defect=qc.defect,
        expected_attribute_results=per_attr,
        expected_quarter_verdict=verdict,
        expected_cross_file_contradiction=contradiction,
    )


def build_all_gold_answers(spec: EngagementSpec) -> list[EngagementGoldAnswer]:
    """Produce all 8 gold answers (2 controls × 4 quarters)."""
    controls: tuple[ControlId, ...] = ("DC-2", "DC-9")
    quarters: tuple[Quarter, ...] = ("Q1", "Q2", "Q3", "Q4")
    return [build_quarter_gold_answer(spec, c, q) for c in controls for q in quarters]


# ── Serialize / load ─────────────────────────────────────────────────


def engagement_gold_answer_to_json(answer: EngagementGoldAnswer) -> str:
    """Serialize with stable key ordering + 2-space indent."""
    return json.dumps(answer.model_dump(mode="json"), indent=2, sort_keys=True)


def load_engagement_gold_answer(path: Path) -> EngagementGoldAnswer:
    """Load + validate an engagement gold-answer JSON file."""
    return EngagementGoldAnswer.model_validate_json(path.read_text())
