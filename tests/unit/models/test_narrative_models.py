"""Validation tests for ``agentic_audit.models.narrative``.

Three boundary models — each rejects malformed input at construction
time so by the time a record reaches the gold writer it's already
structurally correct.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agentic_audit.models.narrative import (
    AttributeNarrative,
    NarrativeRequest,
    NarrativeResponse,
)

UTC_TS = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)


# ---------- NarrativeRequest ----------------------------------------------


def test_narrative_request_happy_path() -> None:
    req = NarrativeRequest(
        control_id="DC-9",
        attribute_id="A",
        quarter="Q1",
        evidence_json='{"foo": "bar"}',
    )
    assert req.control_id == "DC-9"
    assert req.attribute_id == "A"
    assert req.quarter == "Q1"
    assert req.evidence_json == '{"foo": "bar"}'


def test_narrative_request_rejects_empty_evidence_json() -> None:
    with pytest.raises(ValidationError) as exc:
        NarrativeRequest(control_id="DC-9", attribute_id="A", quarter="Q1", evidence_json="")
    assert "evidence_json" in str(exc.value)


def test_narrative_request_rejects_invalid_control_id() -> None:
    with pytest.raises(ValidationError):
        NarrativeRequest(
            control_id="DC-99",  # type: ignore[arg-type]
            attribute_id="A",
            quarter="Q1",
            evidence_json="{}",
        )


def test_narrative_request_rejects_invalid_attribute_id() -> None:
    with pytest.raises(ValidationError):
        NarrativeRequest(
            control_id="DC-9",
            attribute_id="Z",  # type: ignore[arg-type]
            quarter="Q1",
            evidence_json="{}",
        )


# ---------- NarrativeResponse ---------------------------------------------


def test_narrative_response_happy_path() -> None:
    resp = NarrativeResponse(
        narrative_text="Reviewer signed off on row 5.",
        cited_fields=["DC-9 Billing!r5c1"],
        word_count=6,
    )
    assert resp.word_count == 6
    assert resp.cited_fields == ["DC-9 Billing!r5c1"]


def test_narrative_response_cited_fields_defaults_empty() -> None:
    resp = NarrativeResponse(narrative_text="x", word_count=1)
    assert resp.cited_fields == []


def test_narrative_response_rejects_empty_text() -> None:
    with pytest.raises(ValidationError):
        NarrativeResponse(narrative_text="", word_count=0)


def test_narrative_response_rejects_negative_word_count() -> None:
    with pytest.raises(ValidationError):
        NarrativeResponse(narrative_text="x", word_count=-1)


# ---------- AttributeNarrative --------------------------------------------


def _make_attribute_narrative(**overrides: object) -> AttributeNarrative:
    defaults: dict[str, object] = {
        "engagement_id": "alpha-pension-fund-2025",
        "control_id": "DC-9",
        "attribute_id": "A",
        "quarter": "Q1",
        "source_evidence_id": "evidence-row-001",
        "narrative_text": "Reviewer signed off (see DC-9 Billing!r5c1).",
        "cited_fields": ["DC-9 Billing!r5c1"],
        "word_count": 7,
        "prompt_version": "v1.0",
        "model_deployment": "gpt-4o",
        "generation_run_id": "run-2026-05-03-001",
        "generated_at": UTC_TS,
    }
    defaults.update(overrides)
    return AttributeNarrative(**defaults)  # type: ignore[arg-type]


def test_attribute_narrative_happy_path_defaults_fact_check_false() -> None:
    n = _make_attribute_narrative()
    assert n.fact_check_passed is False
    assert n.fact_check_issues == []
    assert n.prompt_version == "v1.0"
    assert n.model_deployment == "gpt-4o"


def test_attribute_narrative_with_fact_check_passed() -> None:
    n = _make_attribute_narrative(fact_check_passed=True)
    assert n.fact_check_passed is True
    assert n.fact_check_issues == []


def test_attribute_narrative_with_fact_check_issues() -> None:
    n = _make_attribute_narrative(
        fact_check_passed=False,
        fact_check_issues=["narrative mentions $5.0M not in evidence"],
    )
    assert n.fact_check_passed is False
    assert n.fact_check_issues == ["narrative mentions $5.0M not in evidence"]


def test_attribute_narrative_rejects_empty_engagement_id() -> None:
    with pytest.raises(ValidationError):
        _make_attribute_narrative(engagement_id="")


def test_attribute_narrative_rejects_empty_prompt_version() -> None:
    with pytest.raises(ValidationError):
        _make_attribute_narrative(prompt_version="")


def test_attribute_narrative_rejects_empty_run_id() -> None:
    with pytest.raises(ValidationError):
        _make_attribute_narrative(generation_run_id="")


def test_attribute_narrative_rejects_invalid_quarter() -> None:
    with pytest.raises(ValidationError):
        _make_attribute_narrative(quarter="Q5")
