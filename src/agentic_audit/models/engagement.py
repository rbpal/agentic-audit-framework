"""EngagementSpec — the v2 corpus top-level schema.

Step 2 Task 01. Replaces v1's per-scenario ``ScenarioSpec`` model with an
engagement-level description: one fictional entity × 4 quarters × 2 controls.

The v2 corpus mirrors real audit chronology — one engagement covering
a calendar year, with each control's state captured per quarter. That's
a better evaluation surface for a ReAct agent that must reason about
state changes between quarters (e.g. "rate changed from Q1 to Q2 — is
there a governing-document amendment backing it?").

See ``privateDocs/step_02_corpus_v2.md`` §4 for the design rationale.

Only the schema lives here. The writers that consume it (engagement
TOC, per-quarter DC-2 / DC-9 W/Ps) arrive in subsequent tasks (02–05);
v1's ``scenario.py`` and ``gold_answer.py`` remain in place until the
cutover in Task 10.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, TypeAlias

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── Type aliases ─────────────────────────────────────────────────────

Quarter: TypeAlias = Literal["Q1", "Q2", "Q3", "Q4"]
ControlId: TypeAlias = Literal["DC-2", "DC-9"]

# One per defect type. ``"none"`` means the quarter is clean for this
# control — every attribute passes. Other values name a specific
# contradiction the agent is expected to detect in that (control, quarter).
QuarterDefect: TypeAlias = Literal[
    "none",
    # DC-9 defects
    "dc9_figure_mismatch",  # TOC billing claim ≠ W/P billing calc (attr C)
    "dc9_rate_change_with_amendment",  # benign — all attrs pass
    "dc9_rate_change_without_amendment",  # governing-doc file absent (attr D)
    # DC-2 defects
    "dc2_variance_no_explanation",  # variance above threshold with no text (attr B)
    "dc2_variance_explanation_inadequate",  # text present, doesn't tie to source (attr C)
    "dc2_variance_boundary",  # upstream feed total ≠ workbook total (attr A)
]

# Which control a given defect can legally attach to. ``None`` means the
# defect is control-agnostic (only ``"none"`` qualifies).
_DEFECT_TO_CONTROL: dict[QuarterDefect, ControlId | None] = {
    "none": None,
    "dc9_figure_mismatch": "DC-9",
    "dc9_rate_change_with_amendment": "DC-9",
    "dc9_rate_change_without_amendment": "DC-9",
    "dc2_variance_no_explanation": "DC-2",
    "dc2_variance_explanation_inadequate": "DC-2",
    "dc2_variance_boundary": "DC-2",
}


# ── Pydantic models ──────────────────────────────────────────────────


class QuarterControlSpec(BaseModel):
    """A single (control × quarter) state entry.

    Each engagement has exactly 8 — 4 quarters × 2 controls — listed in
    ``EngagementSpec.quarters``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    control_id: ControlId
    quarter: Quarter
    defect: QuarterDefect = "none"

    @model_validator(mode="after")
    def _defect_matches_control(self) -> QuarterControlSpec:
        expected = _DEFECT_TO_CONTROL[self.defect]
        if expected is not None and expected != self.control_id:
            raise ValueError(
                f"defect={self.defect!r} is only valid on control_id={expected!r}, "
                f"but this spec has control_id={self.control_id!r}"
            )
        return self


class EngagementSpec(BaseModel):
    """Top-level spec for the v2 corpus.

    One entity, one year, 4 quarters × 2 controls = 8 quarter-control
    entries. The tuple must cover every (control, quarter) combination
    exactly once — enforced by ``_covers_all_quarter_control_combinations``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    entity_name: str = Field(..., min_length=1, max_length=80)
    year: int = Field(..., ge=2000, le=2100)
    seed: int = Field(..., ge=0)
    quarters: tuple[QuarterControlSpec, ...]

    @field_validator("quarters", mode="before")
    @classmethod
    def _coerce_list_to_tuple(cls, v: object) -> object:
        """YAML sequence nodes load as list; pydantic strict mode rejects
        list where tuple is declared. Coerce at the validation boundary.
        """
        if isinstance(v, list):
            return tuple(v)
        return v

    @model_validator(mode="after")
    def _covers_all_quarter_control_combinations(self) -> EngagementSpec:
        expected = {(c, q) for c in ("DC-2", "DC-9") for q in ("Q1", "Q2", "Q3", "Q4")}
        actual = [(qc.control_id, qc.quarter) for qc in self.quarters]

        if len(actual) != len(set(actual)):
            seen: set[tuple[str, str]] = set()
            dupes = sorted({pair for pair in actual if pair in seen or seen.add(pair)})  # type: ignore[func-returns-value]
            raise ValueError(f"engagement.quarters has duplicate (control, quarter) pairs: {dupes}")

        actual_set = set(actual)
        missing = expected - actual_set
        extra = actual_set - expected
        if missing or extra:
            parts: list[str] = []
            if missing:
                parts.append(f"missing: {sorted(missing)}")
            if extra:
                parts.append(f"unexpected: {sorted(extra)}")
            raise ValueError(
                "engagement.quarters must cover all 8 (control, quarter) pairs exactly once; "
                + "; ".join(parts)
            )
        return self


# ── Loaders + lookup ─────────────────────────────────────────────────


def load_engagement(path: Path) -> EngagementSpec:
    """Load and validate a v2 manifest.yaml file.

    Top-level shape:

    .. code-block:: yaml

        engagement:
          entity_name: Alpha Pension Fund
          year: 2025
          seed: 2025
          quarters:
            - control_id: DC-9
              quarter: Q1
              defect: none
            - ...

    Uses ``yaml.safe_load`` — blocks arbitrary-code-execution vectors in
    untrusted YAML.
    """
    data = yaml.safe_load(path.read_text())
    return EngagementSpec(**data["engagement"])


def quarter_control(
    spec: EngagementSpec, control_id: ControlId, quarter: Quarter
) -> QuarterControlSpec:
    """Look up the ``QuarterControlSpec`` for a given (control, quarter).

    Raises ``KeyError`` if the engagement somehow doesn't contain it
    (the ``_covers_all`` validator makes this impossible for a validated
    EngagementSpec, but the raise covers the edge case).
    """
    for qc in spec.quarters:
        if qc.control_id == control_id and qc.quarter == quarter:
            return qc
    raise KeyError(f"No QuarterControlSpec for ({control_id!r}, {quarter!r})")
