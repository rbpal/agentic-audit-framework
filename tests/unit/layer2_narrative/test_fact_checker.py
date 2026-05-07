"""Unit tests for ``agentic_audit.layer2_narrative.fact_checker``.

Pure-deterministic fact-checker — no mocks needed for LLM clients
because there are none. Builds real ``NarrativeResponse`` and
``ExtractedEvidence`` objects, runs ``FactChecker.check()``, asserts
the verdict and the contents of ``issues``.

The integration coverage (every narrative in the 32-row sweep
fact-checked) lands in ``task_07``. This file is the unit gate.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agentic_audit.layer2_narrative.fact_checker import FactChecker
from agentic_audit.models.evidence import (
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)
from agentic_audit.models.narrative import FactCheckResult, NarrativeResponse

UTC_TS = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)


# ---------- fixtures ---------------------------------------------------


def _evidence_with_notes(notes_by_attr: dict[str, str]) -> ExtractedEvidence:
    """Build a DC-9 ExtractedEvidence whose A/B attribute notes carry
    the given content. DC-9 requires all 6 attributes (A-F) per the
    pydantic validator; attributes not in ``notes_by_attr`` get a
    placeholder note that won't collide with test assertions.

    The fact-checker's evidence-as-string flattener serialises the
    whole model to JSON, so anything in `notes` ends up searchable in
    the blob.
    """
    all_attrs = ["A", "B", "C", "D", "E", "F"]
    attrs = [
        AttributeCheck(
            control_id="DC-9",
            attribute_id=attr_id,  # type: ignore[arg-type]
            status="pass",
            evidence_cell_refs=[f"DC9_WP!{attr_id}1"],
            extracted_value={"sample": f"val-{attr_id}"},
            notes=notes_by_attr.get(attr_id, "placeholder"),
        )
        for attr_id in all_attrs
    ]
    return ExtractedEvidence(
        engagement_id="alpha-pension-fund-2025",
        control_id="DC-9",
        quarter="Q1",
        run_id="01J0F7M5XQXM2QYAY8X8X8X8X8",
        extraction_timestamp=UTC_TS,
        preparer=SignOff(initials="AB", role="preparer", date=UTC_TS),
        reviewer=SignOff(initials="CD", role="reviewer", date=UTC_TS),
        attributes=attrs,
        source_bronze_file_hash="a" * 64,
        source_path="/bronze/dc9_Q1_ref.xlsx",
    )


def _narrative(text: str) -> NarrativeResponse:
    return NarrativeResponse(
        narrative_text=text,
        cited_fields=["DC9_WP!A1"],
        word_count=len(text.split()),
    )


# ---------- happy path -------------------------------------------------


def test_passes_when_narrative_quotes_evidence_verbatim() -> None:
    """Every numeric and entity in the narrative appears in the
    evidence blob. ``passed=True``, ``issues=[]``."""
    evidence = _evidence_with_notes(
        {
            "A": "ACME Inc reconciled $1,250 in Q1 with no exceptions.",
            "B": "Reviewer signed off on 2026-01-15.",
        }
    )
    narrative = _narrative("ACME Inc reconciled $1,250 during Q1 with the reviewer's sign-off.")

    result = FactChecker().check(narrative, evidence)

    assert result.passed is True
    assert result.issues == []


# ---------- fabricated numbers (the mandated test from task spec) ------


def test_fails_on_fabricated_number() -> None:
    """The mandated 'unit test with mocked 200-word response' from the
    task index. Narrative says ``$2,500`` but evidence says ``$1,250``
    — ``passed=False``, issue names the fabricated numeric."""
    evidence = _evidence_with_notes({"A": "ACME Inc reconciled $1,250 in Q1 with no exceptions."})
    narrative = _narrative("ACME Inc reconciled $2,500 during Q1.")

    result = FactChecker().check(narrative, evidence)

    assert result.passed is False
    assert any("$2,500" in issue for issue in result.issues), result.issues


def test_fails_on_fabricated_percentage() -> None:
    """Numerics include percentages — a hallucinated 75% should fail
    when evidence has no 75% present."""
    evidence = _evidence_with_notes({"A": "Variance was 12% within tolerance."})
    narrative = _narrative("Variance was 75% within tolerance for Q1.")

    result = FactChecker().check(narrative, evidence)

    assert result.passed is False
    assert any("75%" in issue for issue in result.issues), result.issues


# ---------- fabricated entities ----------------------------------------


def test_fails_on_fabricated_entity() -> None:
    """Narrative names an entity not in evidence — fuzz score below
    threshold so it falls through to ``issues``."""
    evidence = _evidence_with_notes({"A": "ACME Inc reconciled $1,250 in Q1 with no exceptions."})
    # Globex Corp has no fuzzy overlap with anything in the evidence
    # blob (engagement id, ACME, etc.) — should fail.
    narrative = _narrative("Globex Corp reconciled $1,250 during Q1.")

    result = FactChecker().check(narrative, evidence)

    assert result.passed is False
    assert any("Globex" in issue for issue in result.issues), result.issues


# ---------- punctuation/whitespace tolerance ---------------------------


def test_passes_on_minor_punctuation_mismatch() -> None:
    """Narrative says 'ACME Inc' (no comma); evidence says 'ACME, Inc.'
    The fuzz threshold (90) absorbs this — the auditor would not
    consider it a hallucination, and neither should the checker."""
    evidence = _evidence_with_notes({"A": "ACME, Inc. reconciled $1,250 in Q1 with no exceptions."})
    narrative = _narrative("ACME Inc reconciled $1,250 during Q1.")

    result = FactChecker().check(narrative, evidence)

    assert result.passed is True, result.issues


# ---------- FactCheckResult invariant ----------------------------------


def test_fact_check_result_rejects_passed_true_with_issues() -> None:
    """Caller-bug guard: ``passed=True`` + non-empty issues is
    semantically incoherent. Pydantic must reject at construction."""
    with pytest.raises(ValidationError, match="passed=True"):
        FactCheckResult(passed=True, issues=["something"])


def test_fact_check_result_rejects_passed_false_without_issues() -> None:
    """Caller-bug guard: ``passed=False`` with empty issues is
    semantically incoherent — there must be at least one reason."""
    with pytest.raises(ValidationError, match="passed=False"):
        FactCheckResult(passed=False, issues=[])


def test_fact_check_result_accepts_passed_true_with_empty_issues() -> None:
    """Happy-path constructor: ``passed=True, issues=[]`` is valid."""
    result = FactCheckResult(passed=True, issues=[])
    assert result.passed is True
    assert result.issues == []


def test_fact_check_result_accepts_passed_false_with_issues() -> None:
    """Happy-path constructor for the failing case."""
    result = FactCheckResult(passed=False, issues=["bad: 'x'"])
    assert result.passed is False
    assert result.issues == ["bad: 'x'"]


# ---------- upstream guard regression ----------------------------------


def test_empty_narrative_text_rejected_by_upstream_pydantic() -> None:
    """``NarrativeResponse(narrative_text="")`` is rejected by
    pydantic's ``min_length=1`` constraint, so the fact-checker never
    sees a blank narrative. Asserting it as a regression guard so a
    future relaxation of the upstream constraint doesn't silently
    bypass fact-checking."""
    with pytest.raises(ValidationError):
        NarrativeResponse(narrative_text="", cited_fields=[], word_count=0)


# ---------- multiple issues collected ----------------------------------


def test_multiple_issues_all_collected() -> None:
    """The fact-checker doesn't short-circuit on the first issue —
    every fabricated token is named, so the auditor sees the full
    list rather than fixing one and re-discovering the next."""
    evidence = _evidence_with_notes({"A": "ACME Inc reconciled $1,250 in Q1 with no exceptions."})
    narrative = _narrative("Globex Corp reconciled $9,999 during Q3 with FakeCo.")

    result = FactChecker().check(narrative, evidence)

    assert result.passed is False
    # At minimum: the fake number and the fake entities should all be
    # surfaced. We don't assert exact count because Q3 / Q1-style
    # tokens may or may not be flagged depending on what's in the
    # evidence JSON blob — but the three unambiguous fabrications
    # must be present.
    issues_blob = " ".join(result.issues)
    assert "$9,999" in issues_blob
    assert "Globex" in issues_blob
    assert "FakeCo" in issues_blob


# ---------- entity stopword filter (step_05_task_08 calibration) -------


def test_sentence_initial_common_words_are_not_flagged_as_entities() -> None:
    """The first task_07 sweep produced 0/27 pass rate because the
    entity regex was flagging sentence-initial common words ("The",
    "Notes", "No", "Rows") as entities not in evidence. After adding
    the stopword filter, those tokens are skipped and only real
    domain entities reach the grounding check."""
    evidence = _evidence_with_notes({"A": "ACME Inc reconciled $1,250 in Q1 with no exceptions."})
    # Narrative starts every sentence with a stopword and uses several
    # of the common audit-prose words from the stopword list.
    narrative = _narrative(
        "The ACME Inc reconciliation passed. "
        "Notes confirm $1,250 reconciled. "
        "No exceptions noted during Q1. "
        "Reviewer signed off."
    )

    result = FactChecker().check(narrative, evidence)

    # All real entities ("ACME Inc", "Q1") are grounded; the stopwords
    # ("The", "Notes", "No", "Reviewer") are filtered out → passed.
    assert result.passed is True, result.issues


def test_stopword_filter_does_not_mask_real_hallucinated_entity() -> None:
    """Adding stopwords doesn't weaken hallucination detection. A
    fabricated proper noun ("Globex") is still flagged because it's
    not in the stopword list."""
    evidence = _evidence_with_notes({"A": "ACME Inc reconciled $1,250 in Q1 with no exceptions."})
    narrative = _narrative("The Globex reconciliation passed. Notes confirm $1,250.")

    result = FactChecker().check(narrative, evidence)

    assert result.passed is False
    # "Globex" should be flagged; "The" and "Notes" should NOT be.
    issues_blob = " ".join(result.issues)
    assert "Globex" in issues_blob
    assert "'The'" not in issues_blob
    assert "'Notes'" not in issues_blob


def test_stopwords_are_case_sensitive_capitalised_only() -> None:
    """Stopword list contains "The", "No", etc. — capitalised. A
    lowercase "the" or "no" mid-sentence is filtered by the
    capitalised-leading regex itself, so this test mostly documents
    the boundary: only sentence-initial / proper-noun-style
    capitalisations are touched by the stopword logic."""
    from agentic_audit.layer2_narrative.fact_checker import _ENTITY_STOPWORDS

    # All entries are capitalised
    for word in _ENTITY_STOPWORDS:
        assert word[0].isupper(), word
    # Some specific load-bearing entries are present (regression guard)
    assert "The" in _ENTITY_STOPWORDS
    assert "No" in _ENTITY_STOPWORDS
    assert "Notes" in _ENTITY_STOPWORDS
    assert "Rows" in _ENTITY_STOPWORDS
