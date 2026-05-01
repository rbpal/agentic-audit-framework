"""Typed domain models for the agentic audit framework.

v2 engagement schema (post Step 2 cutover). v1's per-scenario schema
(``ScenarioSpec``, ``GoldAnswer``, ``WorkpaperSpec``) was removed in
the cutover commit — history preserves it via git.
"""

from agentic_audit.models.engagement import (
    ControlId,
    EngagementSpec,
    Quarter,
    QuarterControlSpec,
    QuarterDefect,
    load_engagement,
    quarter_control,
)
from agentic_audit.models.engagement_gold_answer import (
    AttributeResult,
    EngagementGoldAnswer,
    FinalVerdict,
    build_all_gold_answers,
    build_quarter_gold_answer,
    engagement_gold_answer_to_json,
    load_engagement_gold_answer,
)
from agentic_audit.models.evidence import (
    AttributeCheck,
    AttributeId,
    CheckStatus,
    ExtractedEvidence,
    SignOff,
    SignOffRole,
)

__all__ = [
    "AttributeCheck",
    "AttributeId",
    "AttributeResult",
    "CheckStatus",
    "ControlId",
    "EngagementGoldAnswer",
    "EngagementSpec",
    "ExtractedEvidence",
    "FinalVerdict",
    "Quarter",
    "QuarterControlSpec",
    "QuarterDefect",
    "SignOff",
    "SignOffRole",
    "build_all_gold_answers",
    "build_quarter_gold_answer",
    "engagement_gold_answer_to_json",
    "load_engagement",
    "load_engagement_gold_answer",
    "quarter_control",
]
