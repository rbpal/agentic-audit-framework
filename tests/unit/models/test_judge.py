"""Validation tests for ``agentic_audit.models.judge``.

Pin every invariant the judge sweep depends on so a misbehaving judge
can't silently corrupt ``audit_dev.gold.eval_outcomes``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentic_audit.models.judge import JudgeResponse


def test_judge_response_pass_with_cited_evidence() -> None:
    r = JudgeResponse(
        verdict="pass",
        confidence=0.9,
        reasoning="Reviewer signoff present in evidence; narrative correctly conveys it.",
        cited_evidence_fields=["reviewer.initials", "reviewer.date"],
    )
    assert r.verdict == "pass"
    assert r.confidence == pytest.approx(0.9)
    assert r.cited_evidence_fields == ["reviewer.initials", "reviewer.date"]


def test_judge_response_fail_with_cited_evidence() -> None:
    r = JudgeResponse(
        verdict="fail",
        confidence=0.95,
        reasoning="Narrative claims management reviewed; evidence shows reviewer cell blank.",
        cited_evidence_fields=["reviewer.initials"],
    )
    assert r.verdict == "fail"


def test_judge_response_uncertain_with_no_cited_evidence_is_valid() -> None:
    """Decision Rule 1 exempts ``uncertain`` from the citation
    requirement — the legitimate "evidence is silent" case."""
    r = JudgeResponse(
        verdict="uncertain",
        confidence=0.4,
        reasoning="Evidence does not unambiguously support pass or fail.",
        cited_evidence_fields=[],
    )
    assert r.verdict == "uncertain"
    assert r.cited_evidence_fields == []


def test_judge_response_rejects_invalid_verdict() -> None:
    """Anything outside {pass, fail, uncertain} is a prompt failure;
    reject at the boundary."""
    with pytest.raises(ValidationError):
        JudgeResponse(
            verdict="maybe",  # type: ignore[arg-type]
            confidence=0.5,
            reasoning="x",
            cited_evidence_fields=["a"],
        )


def test_judge_response_rejects_confidence_above_one() -> None:
    with pytest.raises(ValidationError):
        JudgeResponse(
            verdict="pass",
            confidence=1.01,
            reasoning="x",
            cited_evidence_fields=["a"],
        )


def test_judge_response_rejects_confidence_below_zero() -> None:
    with pytest.raises(ValidationError):
        JudgeResponse(
            verdict="pass",
            confidence=-0.01,
            reasoning="x",
            cited_evidence_fields=["a"],
        )


def test_judge_response_rejects_empty_reasoning() -> None:
    """A bare verdict with no rationale is a prompt failure — reject."""
    with pytest.raises(ValidationError):
        JudgeResponse(
            verdict="pass",
            confidence=0.9,
            reasoning="",
            cited_evidence_fields=["a"],
        )


def test_judge_response_pass_without_cited_evidence_is_rejected() -> None:
    """Decision Rule 1: pass/fail verdicts must cite evidence. Bare
    pass with empty cited_evidence_fields is a hallucinating judge —
    reject loudly with a remediation hint pointing at the prompt rule."""
    with pytest.raises(ValidationError, match="Decision Rule 1"):
        JudgeResponse(
            verdict="pass",
            confidence=0.9,
            reasoning="Looks fine.",
            cited_evidence_fields=[],
        )


def test_judge_response_fail_without_cited_evidence_is_rejected() -> None:
    """Same rule applies to fail verdicts — a fail with no cited
    evidence is unaccountable."""
    with pytest.raises(ValidationError, match="Decision Rule 1"):
        JudgeResponse(
            verdict="fail",
            confidence=0.9,
            reasoning="Wrong somehow.",
            cited_evidence_fields=[],
        )


def test_judge_response_cited_fields_default_empty_list() -> None:
    """Field default is an empty list (only valid for uncertain)."""
    r = JudgeResponse(
        verdict="uncertain",
        confidence=0.3,
        reasoning="Insufficient evidence.",
    )
    assert r.cited_evidence_fields == []
