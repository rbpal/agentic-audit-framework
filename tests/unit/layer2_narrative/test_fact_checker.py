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


def test_utc_timezone_qualifier_is_not_flagged_as_entity() -> None:
    """Regression for the second task_08 calibration sweep finding:
    DC-2 Q2 D and DC-2 Q4 D both failed with the lone issue
    ``"entity not in evidence: 'UTC'"``. The LLM appended a generic
    timezone code to a date in the narrative; ``UTC`` survived the
    entity regex but isn't a domain entity. The fix is to add UTC
    (and GMT/EST/PST) to the stopword list."""
    evidence = _evidence_with_notes(
        {
            "A": (
                "ACME Inc reconciled $1,250 in Q2; the variance review "
                "completed 2025-05-27 and was signed by KL."
            )
        }
    )
    # Reproduce the narrative shape that flagged 'UTC' in the live sweep:
    # date with timezone qualifier + DC-2 reference + extracted value.
    narrative = _narrative(
        "The attribute check for DC-2, attribute D, in Q2 cited evidence "
        "from cell reference DC-2 Variance!r17c1. The extracted value "
        "was 'KL — 2025-05-27 UTC' (see DC-2 Variance!r17c1)."
    )

    result = FactChecker().check(narrative, evidence)

    # The narrative still mentions DC-2, Q2, KL, 2025-05-27 — all
    # in evidence. After the UTC stopword fix, no FAIL.
    assert result.passed is True, result.issues


def test_other_timezone_qualifiers_are_also_filtered() -> None:
    """``GMT``, ``EST``, ``PST`` are added to the stopword list along
    with ``UTC`` because they are the same class of generic
    timezone-code-appended-to-a-date pattern. Documents the family
    so a future LLM that picks a different timezone abbreviation
    doesn't surprise us."""
    from agentic_audit.layer2_narrative.fact_checker import _ENTITY_STOPWORDS

    for tz_code in ("UTC", "GMT", "EST", "PST"):
        assert tz_code in _ENTITY_STOPWORDS, tz_code


def test_sentence_initial_however_is_not_flagged_as_entity() -> None:
    """Regression for the v1.1 baseline sweep (judge_run_id
    FBFCC5B0A6D48751910CACDD4F9EC011). Every cross-file-dependent
    attribute narrative (DC-9.C in Q1/Q2/Q4) introduced a qualifying
    clause starting with "However,". The entity regex captured
    capitalised ``However`` as a proper-noun token; it isn't one.
    The fix is to add common sentence-initial transition adverbs
    (However, Therefore, Furthermore, etc.) to the stopword list.
    """
    evidence = _evidence_with_notes(
        {
            "C": (
                "DC-9 Billing check status pass; asset 337995108.0, "
                "fee 844987.0, rate 0.0025; null notes."
            )
        }
    )
    # Reproduce the v1.1 narrative shape that flagged 'However' in the
    # live sweep: qualifying clause about cross-file dependency.
    narrative = _narrative(
        "The attribute DC-9.C was tested using evidence from "
        "DC-9 Billing!r8c1. The workpaper-internal check status is "
        "'pass' (see DC-9 Billing!r8c1). However, the overall "
        "conclusion depends on cross-file reconciliation, which is "
        "not evaluated within this workpaper."
    )

    result = FactChecker().check(narrative, evidence)

    # After the However stopword fix, the narrative should pass —
    # all real entities ("DC-9", "DC-9 Billing") are grounded.
    assert result.passed is True, result.issues


def test_transition_adverbs_are_all_in_the_stopword_family() -> None:
    """Documents the full transition-adverb family at the test layer
    so a future LLM that picks a different transition word ("Moreover"
    instead of "However") doesn't surprise us with a fresh FP."""
    from agentic_audit.layer2_narrative.fact_checker import _ENTITY_STOPWORDS

    for adverb in (
        "However",
        "Therefore",
        "Furthermore",
        "Additionally",
        "Moreover",
        "Consequently",
        "Subsequently",
        "Nevertheless",
        "Accordingly",
        "Specifically",
        "Particularly",
    ):
        assert adverb in _ENTITY_STOPWORDS, adverb


def test_toc_audit_concept_abbreviation_is_not_flagged_as_entity() -> None:
    """Regression for the v1.1 sweep: every cross-file-dependent
    attribute narrative references "engagement TOC" as part of the
    qualifying clause ("the overall conclusion depends on cross-file
    reconciliation against the engagement TOC"). The TOC isn't
    workpaper-content — it's a separate audit artifact the narrative
    intentionally names to acknowledge cross-file scope. FactChecker
    shouldn't require TOC content to appear in workpaper-only
    evidence. Fix: TOC added to the stopword list.
    """
    evidence = _evidence_with_notes(
        {"C": ("DC-2 Variance check status pass; 4 rows checked in Q1; null notes.")}
    )
    # Reproduce the v1.1 narrative shape that flagged 'TOC' in the
    # live sweep: cross-file qualification language for DC-2.C.
    # Fixture is hard-coded to Q1 so the narrative uses Q1 too;
    # the test is about TOC, not quarter scope.
    narrative = _narrative(
        "The attribute DC-2.C was tested in Q1. The workpaper-internal "
        "check status is 'pass' (see DC-2 Variance!r10c7). The overall "
        "conclusion depends on cross-file reconciliation against the "
        "engagement TOC, which is not evaluated within this workpaper."
    )

    result = FactChecker().check(narrative, evidence)

    assert result.passed is True, result.issues


# ---------- numeric percent ↔ decimal equivalence ----------------------


def test_numeric_variants_percent_input_emits_three_equivalence_classes() -> None:
    """A percent in the narrative expands into THREE classes of
    equivalent forms an evidence blob might hold:

    1. Bare-number form (``40.0%`` → ``"40.0"``) — the DC-9 F case
       where silver stores ``"effective_pcts":[40.0, 30.0, 30.0]``
       with unit semantics carried by the field name.
    2. Bare-integer form (``40%`` → ``"40"``) — same as (1) but for
       evidence storing whole percents as int.
    3. Decimal-equivalent form (``40.0%`` → ``"0.4"``) — silver
       stores fraction-of-1, narrative humanises to percent.

    Surfaced empirically by three iterations of the task_07 → task_08
    calibration sweep."""
    from agentic_audit.layer2_narrative.fact_checker import _numeric_variants

    # DC-9 F shape: bare percent number (with .0) + integer + decimal-eq
    assert _numeric_variants("40.0%") == ["40.0", "40", "0.4"]
    assert _numeric_variants("30.0%") == ["30.0", "30", "0.3"]
    # 100.0% → bare "100.0" + integer "100" + decimal "1.0" + integer "1"
    assert _numeric_variants("100.0%") == ["100.0", "100", "1.0", "1"]
    # Whole percent without explicit decimal: same shape
    assert _numeric_variants("40%") == ["40.0", "40", "0.4"]
    # Non-round percent: bare-number form (75.0) + bare-integer form (75) + decimal (0.75)
    assert _numeric_variants("75%") == ["75.0", "75", "0.75"]
    # Fractional percent: bare "12.5" (no integer form since not whole) + decimal
    assert _numeric_variants("12.5%") == ["12.5", "0.125"]


def test_numeric_variants_decimal_to_percent() -> None:
    """Reverse direction: a small decimal in the narrative should
    expand to its percent form. Only applied to (0, 1] range —
    outside that, decimal-as-percentage is unsafe."""
    from agentic_audit.layer2_narrative.fact_checker import _numeric_variants

    # Whole percent: both "40%" and "40.0%" forms emitted
    assert _numeric_variants("0.4") == ["40%", "40.0%"]
    assert _numeric_variants("1.0") == ["100%", "100.0%"]
    # Trailing-zero forms canonicalise via Decimal
    assert _numeric_variants("0.40") == ["40%", "40.0%"]
    # Fractional percent: canonical form only
    assert _numeric_variants("0.75") == ["75%", "75.0%"]


def test_numeric_variants_no_translation_for_dollar_or_integer_or_large_decimal() -> None:
    """Dollar amounts, plain integers, and decimals > 1 don't get
    phantom translations. False-equating ``$5`` to ``5%`` would mask
    real hallucinations."""
    from agentic_audit.layer2_narrative.fact_checker import _numeric_variants

    assert _numeric_variants("$2,500") == []
    assert _numeric_variants("150") == []
    assert _numeric_variants("5") == []
    # Decimal > 1 — narrative ``1.5`` is not safely equivalent to
    # ``150%`` (could be rate, count, or anything)
    assert _numeric_variants("1.5") == []


def test_numeric_variants_handles_garbage_input() -> None:
    """Bad input shouldn't crash — return empty list and let the
    direct-string-match fallback handle it."""
    from agentic_audit.layer2_narrative.fact_checker import _numeric_variants

    assert _numeric_variants("abc%") == []
    assert _numeric_variants("12.34.56") == []


def test_numeric_grounded_via_decimal_equivalence_passes_dc9_f_pattern() -> None:
    """End-to-end on the DC-9 attribute F failure pattern: narrative
    cites multiple percents, evidence stores the equivalent decimals.
    After the percent/decimal calibration, all four numerics in this
    pattern resolve to PASS."""
    evidence = _evidence_with_notes(
        {
            "F": (
                "DC-9 Billing!r26c2 contains rate 0.4. "
                "DC-9 Billing!r27c2 contains rate 0.3. "
                "DC-9 Billing!r28c2 contains rate 1.0."
            )
        }
    )
    narrative = _narrative(
        "The attribute check for DC-9 attribute F was conducted using "
        "evidence from cells DC-9 Billing!r26c2, DC-9 Billing!r27c2, "
        "and DC-9 Billing!r28c2. The extracted values for effective "
        "percentages were 40.0%, 30.0%, and 100.0%."
    )

    result = FactChecker().check(narrative, evidence)

    # All numerics ground via decimal equivalence; entities like
    # "DC-9", "Billing" are real and present in evidence.
    assert result.passed is True, result.issues


def test_numeric_grounded_does_not_false_match_via_substring() -> None:
    """Critical false-positive guard: narrative ``40%`` expands to
    decimal variant ``0.4``, but evidence storing a *different* value
    ``0.45`` must NOT be accepted as grounding for ``40%``. The
    word-boundary regex prevents the substring trap."""
    evidence = _evidence_with_notes({"A": "DC-9 Billing!r26c2 contains rate 0.45."})
    narrative = _narrative("DC-9 Billing!r26c2 reflects an effective rate of 40%.")

    result = FactChecker().check(narrative, evidence)

    # 40% should NOT match 0.45 (despite "0.4" being a prefix
    # substring of "0.45"). The narrative is making a false claim.
    assert result.passed is False
    assert any("40%" in issue for issue in result.issues), result.issues


def test_numeric_grounded_real_hallucination_still_flagged() -> None:
    """Adding decimal equivalence doesn't weaken hallucination
    detection. Narrative claims ``75%``, evidence has decimal ``0.5``
    (= 50%, not 75%) — should fail."""
    evidence = _evidence_with_notes({"A": "DC-9 Billing!r26c2 contains rate 0.5."})
    narrative = _narrative("DC-9 Billing!r26c2 reflects an effective rate of 75%.")

    result = FactChecker().check(narrative, evidence)

    assert result.passed is False
    assert any("75%" in issue for issue in result.issues), result.issues


def test_numeric_grounded_evidence_with_percent_narrative_with_decimal() -> None:
    """Reverse case: silver stores the percent form (rare but
    possible if upstream came in that way), narrative writes the
    decimal. Both should ground."""
    evidence = _evidence_with_notes({"A": "DC-9 Billing!r26c2 contains rate 40%."})
    narrative = _narrative("DC-9 Billing!r26c2 reflects an effective rate of 0.4.")

    result = FactChecker().check(narrative, evidence)

    assert result.passed is True, result.issues


def test_numeric_grounded_passes_dc9_f_actual_silver_json_shape() -> None:
    """End-to-end regression for the EXACT silver-side JSON shape
    that surfaced the DC-9 F failure cluster in the live re-fact-check
    on 2026-05-09. Evidence stores percent VALUES as bare numbers
    (``"effective_pcts":[40.0, 30.0, 30.0], "total":100.0``); the LLM
    correctly renders them as ``40.0%, 30.0%, 30.0%, totaling 100.0%``.
    Without the bare-number percent variant, all four numerics flag
    as ungrounded — that was the residual 4-FAIL cluster after PRs
    #71 / #73 / #74."""
    # Use 2025-02-09 as preparer date so the narrative's date
    # reference is grounded in evidence (matches the live DC-9 F
    # gold rows which all show the preparer date in the narrative).
    preparer_date = datetime(2025, 2, 9, 12, 0, 0, tzinfo=UTC)
    attrs = [
        AttributeCheck(
            control_id="DC-9",
            attribute_id=attr_id,  # type: ignore[arg-type]
            status="pass",
            evidence_cell_refs=(
                ["DC-9 Billing!r26c2", "DC-9 Billing!r27c2", "DC-9 Billing!r28c2"]
                if attr_id == "F"
                else [f"DC9_WP!{attr_id}1"]
            ),
            extracted_value=(
                {"effective_pcts": [40.0, 30.0, 30.0], "total": 100.0}
                if attr_id == "F"
                else {"sample": f"val-{attr_id}"}
            ),
            notes=None if attr_id == "F" else "placeholder",
        )
        for attr_id in ["A", "B", "C", "D", "E", "F"]
    ]
    evidence = ExtractedEvidence(
        engagement_id="alpha-pension-fund-2025",
        control_id="DC-9",
        quarter="Q1",
        run_id="01J0F7M5XQXM2QYAY8X8X8X8X8",
        extraction_timestamp=UTC_TS,
        preparer=SignOff(initials="FV", role="preparer", date=preparer_date),
        reviewer=SignOff(initials="CD", role="reviewer", date=preparer_date),
        attributes=attrs,
        source_bronze_file_hash="a" * 64,
        source_path="/bronze/dc9_Q1_ref.xlsx",
    )
    # The actual narrative shape gpt-4o produced for DC-9 F
    narrative = _narrative(
        "The attribute check for DC-9, attribute F, in Q1 was prepared "
        "by FV on 2025-02-09 (see DC-9 Billing!r26c2, DC-9 Billing!r27c2, "
        "DC-9 Billing!r28c2). The extracted values for effective "
        "percentages were 40.0%, 30.0%, and 30.0%, totaling 100.0%."
    )

    result = FactChecker().check(narrative, evidence)

    # Every numeric resolves via the bare-number percent variant;
    # entities (DC-9, Billing, FV) are all in evidence.
    assert result.passed is True, result.issues


# ---------- @traced_function decorator on FactChecker.check ------------


def test_fact_checker_check_emits_span_start_and_span_end(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``FactChecker.check`` is one of the four Layer-2 entry points
    that ``step_05_task_09_coverage_tracing`` mandated be wrapped in
    ``@traced_function``. Verify the decorator is present by asserting
    the ``span_start`` / ``span_end`` log records emit on the
    ``agentic_audit.trace`` logger when ``check()`` is invoked.

    The other three entry points (``generate``, ``write_narrative``,
    ``silver_reader.read``) had this decorator from the start; ``check``
    was added in task_09 closeout."""
    import logging

    evidence = _evidence_with_notes({"A": "ACME Inc reconciled $1,250 in Q1 with no exceptions."})
    narrative = _narrative("ACME Inc reconciled $1,250 during Q1.")

    with caplog.at_level(logging.INFO, logger="agentic_audit.trace"):
        FactChecker().check(narrative, evidence)

    span_records = [
        rec
        for rec in caplog.records
        if rec.name == "agentic_audit.trace"
        and getattr(rec, "span", None) == "layer2.fact_checker.check"
    ]
    span_messages = {rec.message for rec in span_records}
    # Both span_start and span_end must fire — proves the decorator
    # is wrapping the method, not just imported but unused.
    assert "span_start" in span_messages
    assert "span_end" in span_messages
