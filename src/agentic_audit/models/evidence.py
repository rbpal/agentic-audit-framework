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

from pydantic import BaseModel, Field, field_validator, model_validator

from agentic_audit.models.engagement import ControlId, Quarter

AttributeId: TypeAlias = Literal["A", "B", "C", "D"]
CheckStatus: TypeAlias = Literal["pass", "fail", "n/a"]
SignOffRole: TypeAlias = Literal["preparer", "reviewer"]


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

    Carries exactly four `AttributeCheck` entries (A, B, C, D) and a
    SHA256 hash of the source bronze file for lineage.
    """

    engagement_id: str = Field(min_length=1)
    control_id: ControlId
    quarter: Quarter
    run_id: str = Field(min_length=1)
    extraction_timestamp: datetime
    preparer: SignOff
    reviewer: SignOff
    attributes: list[AttributeCheck] = Field(min_length=4, max_length=4)
    source_bronze_file_hash: str = Field(min_length=1)

    @field_validator("attributes")
    @classmethod
    def four_unique_attribute_ids(cls, v: list[AttributeCheck]) -> list[AttributeCheck]:
        ids = sorted(a.attribute_id for a in v)
        if ids != ["A", "B", "C", "D"]:
            raise ValueError(f"attributes must cover A,B,C,D exactly; got {ids}")
        return v

    @model_validator(mode="after")
    def control_id_consistent(self) -> ExtractedEvidence:
        for a in self.attributes:
            if a.control_id != self.control_id:
                raise ValueError(
                    f"attribute {a.attribute_id} has control_id={a.control_id} "
                    f"but ExtractedEvidence.control_id={self.control_id}"
                )
        return self


__all__ = [
    "AttributeCheck",
    "AttributeId",
    "CheckStatus",
    "ExtractedEvidence",
    "SignOff",
    "SignOffRole",
]
