"""Pydantic models for Layer 1 deterministic extraction.

The contract for everything the orchestrator emits and the silver writer
persists. Validation happens at the layer boundary — by the time a record
reaches `audit_dev.silver.evidence`, every field has passed schema and
cross-field checks.

Re-exports `ControlId` and `Quarter` from `models.engagement` so callers
have a single import surface for Step 4 types.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field, model_validator

from agentic_audit.models.engagement import ControlId, Quarter

AttributeId: TypeAlias = Literal["A", "B", "C", "D", "E", "F"]
CheckStatus: TypeAlias = Literal["pass", "fail", "n/a"]
SignOffRole: TypeAlias = Literal["preparer", "reviewer"]

# Per-control attribute coverage. DC-2 carries 4 attributes (A-D),
# DC-9 carries 6 attributes (A-F). Source of truth: the engagement TOC
# files at eval/gold_scenarios/tocs/*.json.
ATTRIBUTES_PER_CONTROL: dict[str, list[str]] = {
    "DC-2": ["A", "B", "C", "D"],
    "DC-9": ["A", "B", "C", "D", "E", "F"],
}


class SignOff(BaseModel):
    """Auditor sign-off — preparer or reviewer."""

    initials: str = Field(min_length=2, max_length=4)
    role: SignOffRole
    date: datetime


class AttributeCheck(BaseModel):
    """Outcome of one (control, attribute) check against bronze rows."""

    control_id: ControlId
    attribute_id: AttributeId
    status: CheckStatus
    evidence_cell_refs: list[str] = Field(default_factory=list)
    extracted_value: Any | None = None
    notes: str | None = None


class ExtractedEvidence(BaseModel):
    """Layer 1 output for one (engagement, control, quarter) triple.

    Carries one `AttributeCheck` per attribute the control defines:
    - DC-2 → A, B, C, D (4 entries)
    - DC-9 → A, B, C, D, E, F (6 entries)

    Plus a SHA256 hash of the source bronze file for lineage.
    """

    engagement_id: str = Field(min_length=1)
    control_id: ControlId
    quarter: Quarter
    run_id: str = Field(min_length=1)
    extraction_timestamp: datetime
    preparer: SignOff
    reviewer: SignOff
    attributes: list[AttributeCheck] = Field(min_length=4, max_length=6)
    source_bronze_file_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def attributes_match_control(self) -> ExtractedEvidence:
        """Cross-field validation:

        1. Every nested ``AttributeCheck.control_id`` matches the parent.
        2. The attribute IDs cover exactly what the control defines —
           ``ATTRIBUTES_PER_CONTROL[control_id]`` — no gaps, no extras,
           no duplicates.
        """
        for a in self.attributes:
            if a.control_id != self.control_id:
                raise ValueError(
                    f"attribute {a.attribute_id} has control_id={a.control_id} "
                    f"but ExtractedEvidence.control_id={self.control_id}"
                )
        expected = ATTRIBUTES_PER_CONTROL[self.control_id]
        got = sorted(a.attribute_id for a in self.attributes)
        if got != expected:
            raise ValueError(
                f"control_id={self.control_id} requires attributes {expected}; got {got}"
            )
        return self


__all__ = [
    "ATTRIBUTES_PER_CONTROL",
    "AttributeCheck",
    "AttributeId",
    "CheckStatus",
    "ExtractedEvidence",
    "SignOff",
    "SignOffRole",
]
