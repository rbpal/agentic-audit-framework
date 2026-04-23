"""ScenarioSpec — the typed input for synthetic workbook generation.

One spec drives one synthetic .xlsx + one gold .json. The generator, writer,
and gold-JSON emitter all consume ScenarioSpec instances and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, TypeAlias

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Type aliases — kept at module level for reuse in Task 5 gold JSON schema.
Quarter: TypeAlias = Literal["Q1", "Q3"]
ControlId: TypeAlias = Literal["DC-9", "DC-2"]
PatternType: TypeAlias = Literal["signoff_with_tieout", "variance_detection"]
ExpectedOutcome: TypeAlias = Literal["pass", "exception"]
ExceptionType: TypeAlias = Literal[
    "none",
    "signoff_missing",
    "figure_mismatch",
    "billing_rate_change_with_amendment",
    "billing_rate_change_without_amendment",
    "variance_above_threshold_no_explanation",
    "variance_explanation_inadequate",
    "boundary_edge_case",
]

# Supporting-workpaper types a scenario may reference. Extended in Task 12+
# when concrete writers arrive; Task 11 reserves the initial enum values.
#
# * ``billing_calculation`` — used by DC-9 (sign-off with tie-out) to back
#   figure_mismatch / billing_rate_change_* scenarios.
# * ``governing_document_amendment`` — used by DC-9 rate-change scenarios;
#   presence or absence of the amendment file aligns with the tickmark.
# * ``variance_analysis`` — used by DC-2 (variance detection) to back
#   variance_above_threshold_no_explanation / _inadequate / boundary scenarios.
WorkpaperType: TypeAlias = Literal[
    "billing_calculation",
    "governing_document_amendment",
    "variance_analysis",
]

# Pattern <-> control consistency. Extending to a new control = one entry here
# plus one Literal addition above.
_CONTROL_TO_PATTERN: dict[ControlId, PatternType] = {
    "DC-9": "signoff_with_tieout",
    "DC-2": "variance_detection",
}

# Exception-attribute mapping: for exception scenarios, which attribute
# letter gets the failing tickmark (and the "fail" flag in the gold JSON).
# Single source of truth — consumed by both generator (Task 3) and
# gold_answer (Task 5). Keeping it on the models side avoids a circular
# import between generator and models.
_EXCEPTION_ATTRIBUTE: dict[ExceptionType, str] = {
    "none": "",
    "signoff_missing": "B",
    "figure_mismatch": "D",
    "billing_rate_change_with_amendment": "D",
    "billing_rate_change_without_amendment": "D",
    "variance_above_threshold_no_explanation": "B",
    "variance_explanation_inadequate": "C",
    "boundary_edge_case": "A",
}


class WorkpaperSpec(BaseModel):
    """A single supporting W/P that a scenario's TOC references.

    Declares structural metadata — *which file* backs *which TOC reference
    code*. The file's content (amounts, periods, tickmark-worthy values)
    is derived at generation time from the parent ``ScenarioSpec`` —
    future W/P writers (Task 12+) read ``scenario.exception_type`` and
    emit content that either agrees or disagrees with the TOC per the
    cross-file consistency rules (Q7.13 Option B).

    Fields:

    * ``type`` — the writer module selector.
    * ``filename`` — relative name under
      ``eval/gold_scenarios/workpapers/<scenario_id>/``. No `_ref` suffix;
      supporting W/Ps are not TOCs.
    * ``toc_reference_code`` — the code the TOC cell already prints (e.g.
      ``"DC-5.7"``, ``"6.03"``). The agent (Step 2+) is expected to
      extract this code and locate the backing file.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    type: WorkpaperType
    filename: str = Field(..., min_length=1, max_length=80)
    toc_reference_code: str = Field(..., min_length=1, max_length=20)


class ScenarioSpec(BaseModel):
    """A single synthetic audit scenario specification.

    Drives deterministic generation of one .xlsx + one gold .json pair,
    plus any supporting W/Ps declared in ``workpapers`` (Task 12+).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    scenario_id: str = Field(..., min_length=5, max_length=80)
    control_id: ControlId
    pattern_type: PatternType
    quarter: Quarter
    expected_outcome: ExpectedOutcome
    exception_type: ExceptionType = "none"
    seed: int = Field(..., ge=0)
    workpapers: tuple[WorkpaperSpec, ...] = ()

    @field_validator("workpapers", mode="before")
    @classmethod
    def _coerce_workpapers_list_to_tuple(cls, v: object) -> object:
        """YAML ``safe_load`` returns list[dict] for sequence nodes; pydantic
        strict mode rejects list where tuple is declared. Coerce list → tuple
        at the validation boundary so manifest.yaml keeps its natural shape.
        """
        if isinstance(v, list):
            return tuple(v)
        return v

    @model_validator(mode="after")
    def _pattern_matches_control(self) -> ScenarioSpec:
        expected = _CONTROL_TO_PATTERN[self.control_id]
        if self.pattern_type != expected:
            raise ValueError(
                f"control_id={self.control_id!r} requires "
                f"pattern_type={expected!r}, got {self.pattern_type!r}"
            )
        return self

    @model_validator(mode="after")
    def _exception_matches_outcome(self) -> ScenarioSpec:
        if self.expected_outcome == "pass" and self.exception_type != "none":
            raise ValueError(
                f"expected_outcome='pass' requires exception_type='none', "
                f"got {self.exception_type!r}"
            )
        if self.expected_outcome == "exception" and self.exception_type == "none":
            raise ValueError("expected_outcome='exception' requires a specific exception_type")
        return self

    @model_validator(mode="after")
    def _workpaper_filenames_unique(self) -> ScenarioSpec:
        filenames = [wp.filename for wp in self.workpapers]
        if len(filenames) != len(set(filenames)):
            raise ValueError(
                f"workpaper filenames must be unique within a scenario, got {filenames!r}"
            )
        return self

    @model_validator(mode="after")
    def _workpaper_reference_codes_unique(self) -> ScenarioSpec:
        codes = [wp.toc_reference_code for wp in self.workpapers]
        if len(codes) != len(set(codes)):
            raise ValueError(
                f"workpaper toc_reference_code values must be unique within a "
                f"scenario, got {codes!r}"
            )
        return self


def load_manifest(path: Path) -> list[ScenarioSpec]:
    """Load and validate every scenario in a manifest.yaml file.

    Uses yaml.safe_load — blocks arbitrary-code-execution vectors in
    untrusted YAML input.
    """
    data = yaml.safe_load(path.read_text())
    return [ScenarioSpec(**scenario) for scenario in data["scenarios"]]


def pick_exception_attribute(spec: ScenarioSpec) -> str:
    """Return the attribute letter (A–F) that fails on this scenario, or ``""``.

    Single source of truth for exception-attribute mapping. Consumed by
    both the generator (Task 3 — tickmark placement) and the gold-answer
    builder (Task 5 — ``expected_per_attribute_result`` fail-flag). If
    this mapping ever changes, BOTH the workbook tickmarks and the gold
    JSON update atomically.
    """
    return _EXCEPTION_ATTRIBUTE[spec.exception_type]
