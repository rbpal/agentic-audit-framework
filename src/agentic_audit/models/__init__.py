"""Typed domain models for the agentic audit framework."""

from agentic_audit.models.gold_answer import (
    AttributeResult,
    FinalVerdict,
    GoldAnswer,
    build_gold_answer,
    gold_answer_to_json,
    load_gold_answer,
)
from agentic_audit.models.scenario import (
    ControlId,
    ExceptionType,
    ExpectedOutcome,
    PatternType,
    Quarter,
    ScenarioSpec,
    WorkpaperSpec,
    WorkpaperType,
    load_manifest,
)

__all__ = [
    "AttributeResult",
    "ControlId",
    "ExceptionType",
    "ExpectedOutcome",
    "FinalVerdict",
    "GoldAnswer",
    "PatternType",
    "Quarter",
    "ScenarioSpec",
    "WorkpaperSpec",
    "WorkpaperType",
    "build_gold_answer",
    "gold_answer_to_json",
    "load_gold_answer",
    "load_manifest",
]
