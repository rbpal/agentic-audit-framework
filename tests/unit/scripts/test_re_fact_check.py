"""Unit tests for ``scripts/re_fact_check.py``.

Mocks the warehouse connection entirely — no live calls, no
``databricks-sql-connector`` dep required. Verifies the loop-shape
of ``re_fact_check`` (read existing rows → re-evaluate → UPDATE
each), the silver-cache contract, and the dry-run path.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# `scripts/` isn't a package on PYTHONPATH by default; add it explicitly.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from re_fact_check import re_fact_check  # type: ignore[import-not-found]  # noqa: E402

from agentic_audit.layer2_narrative.fact_checker import FactChecker  # noqa: E402
from agentic_audit.models.evidence import (  # noqa: E402
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)

UTC_TS = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)


def _fake_evidence() -> ExtractedEvidence:
    """DC-9 evidence with notes that contain real domain entities so a
    well-formed narrative will pass the post-stopwords fact-checker."""
    attrs = [
        AttributeCheck(
            control_id="DC-9",
            attribute_id=a,  # type: ignore[arg-type]
            status="pass",
            evidence_cell_refs=[f"DC9_WP!{a}1"],
            extracted_value={"sample": f"val-{a}"},
            notes=f"ACME Inc reconciled $1,250 for attr {a}",
        )
        for a in ["A", "B", "C", "D", "E", "F"]
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


def _build_conn_factory(
    *,
    select_rows: list[dict],
    update_capture: list[tuple[str, dict]],
):
    """Build a mock conn_factory that:

    - Returns ``select_rows`` from a SELECT-shaped query
    - Captures every UPDATE call into ``update_capture``
    """

    def factory():
        cur = MagicMock()
        cur.description = [
            ("control_id",),
            ("quarter",),
            ("attribute_id",),
            ("narrative_text",),
            ("cited_fields",),
            ("word_count",),
            ("fact_check_passed",),
        ]

        def execute(sql, params=None):
            sql_upper = sql.strip().upper()
            if sql_upper.startswith("SELECT"):
                # Build the rowset to fetchall()
                rowset = [
                    (
                        r["control_id"],
                        r["quarter"],
                        r["attribute_id"],
                        r["narrative_text"],
                        r["cited_fields"],
                        r["word_count"],
                        r["fact_check_passed"],
                    )
                    for r in select_rows
                ]
                cur.fetchall.return_value = rowset
            elif sql_upper.startswith("UPDATE"):
                update_capture.append((sql, params))
            else:
                raise AssertionError(f"unexpected SQL shape: {sql_upper[:40]}")

        cur.execute.side_effect = execute
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False

        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False

        @contextmanager
        def cm():
            yield conn

        return cm()

    return factory


def test_re_fact_check_evaluates_every_loaded_row_and_writes_back(
    capsys: pytest.CaptureFixture,
) -> None:
    """Loop reads N rows, re-evaluates each via FactChecker, and runs
    one UPDATE per row. Returns ``(n_evaluated, n_passed, n_changed)``.
    """
    select_rows = [
        {
            "control_id": "DC-9",
            "quarter": "Q1",
            "attribute_id": "A",
            "narrative_text": "ACME Inc reconciled $1,250 in Q1 with no exceptions.",
            "cited_fields": ["DC9_WP!A1"],
            "word_count": 9,
            "fact_check_passed": False,  # was failing pre-calibration
        },
        {
            "control_id": "DC-9",
            "quarter": "Q1",
            "attribute_id": "B",
            "narrative_text": "ACME Inc reconciled $1,250 for attr B.",
            "cited_fields": ["DC9_WP!B1"],
            "word_count": 7,
            "fact_check_passed": False,
        },
    ]
    update_capture: list[tuple[str, dict]] = []
    factory = _build_conn_factory(select_rows=select_rows, update_capture=update_capture)

    silver_reader = MagicMock()
    silver_reader.read.return_value = _fake_evidence()
    fact_checker = FactChecker()

    n_evaluated, n_passed, n_changed = re_fact_check(
        engagement_id="alpha-pension-fund-2025",
        prompt_version="v1.0",
        silver_reader=silver_reader,
        fact_checker=fact_checker,
        conn_factory=factory,
        dry_run=False,
    )

    assert n_evaluated == 2
    # Both narratives quote evidence verbatim; should pass post-stopwords
    assert n_passed == 2
    # Both flipped from False (prior) to True (now)
    assert n_changed == 2
    # One UPDATE per row
    assert len(update_capture) == 2
    # Each UPDATE carries the post-evaluation verdict + the composite key
    for sql, params in update_capture:
        assert sql.strip().upper().startswith("UPDATE")
        assert params["passed"] is True
        assert params["engagement_id"] == "alpha-pension-fund-2025"
        assert params["prompt_version"] == "v1.0"
        # Issues serialised as JSON for the from_json cast — empty array
        assert json.loads(params["issues_json"]) == []


def test_re_fact_check_dry_run_does_not_issue_update_statements() -> None:
    """``--dry-run`` reads + evaluates but skips the UPDATE."""
    select_rows = [
        {
            "control_id": "DC-9",
            "quarter": "Q1",
            "attribute_id": "A",
            "narrative_text": "ACME Inc reconciled $1,250 in Q1.",
            "cited_fields": ["DC9_WP!A1"],
            "word_count": 7,
            "fact_check_passed": False,
        },
    ]
    update_capture: list[tuple[str, dict]] = []
    factory = _build_conn_factory(select_rows=select_rows, update_capture=update_capture)

    silver_reader = MagicMock()
    silver_reader.read.return_value = _fake_evidence()

    n_evaluated, _n_passed, _n_changed = re_fact_check(
        engagement_id="alpha-pension-fund-2025",
        prompt_version="v1.0",
        silver_reader=silver_reader,
        fact_checker=FactChecker(),
        conn_factory=factory,
        dry_run=True,
    )

    assert n_evaluated == 1
    # No UPDATE statements issued
    assert update_capture == []


def test_re_fact_check_caches_silver_per_triple() -> None:
    """Two narratives under the same (engagement, control, quarter) =
    one silver read, not two. Same caching contract as the sweep
    driver — locked here to prevent regression."""
    select_rows = [
        {
            "control_id": "DC-9",
            "quarter": "Q1",
            "attribute_id": "A",
            "narrative_text": "ACME Inc reconciled $1,250 in Q1.",
            "cited_fields": ["DC9_WP!A1"],
            "word_count": 7,
            "fact_check_passed": False,
        },
        {
            "control_id": "DC-9",
            "quarter": "Q1",
            "attribute_id": "B",
            "narrative_text": "ACME Inc reconciled $1,250 for attr B.",
            "cited_fields": ["DC9_WP!B1"],
            "word_count": 7,
            "fact_check_passed": False,
        },
        {
            # Different quarter → distinct cache key → second silver read
            "control_id": "DC-9",
            "quarter": "Q2",
            "attribute_id": "A",
            "narrative_text": "ACME Inc reconciled $1,250 for attr A.",
            "cited_fields": ["DC9_WP!A1"],
            "word_count": 7,
            "fact_check_passed": False,
        },
    ]
    update_capture: list[tuple[str, dict]] = []
    factory = _build_conn_factory(select_rows=select_rows, update_capture=update_capture)

    silver_reader = MagicMock()
    silver_reader.read.return_value = _fake_evidence()

    re_fact_check(
        engagement_id="alpha-pension-fund-2025",
        prompt_version="v1.0",
        silver_reader=silver_reader,
        fact_checker=FactChecker(),
        conn_factory=factory,
        dry_run=True,
    )

    # 2 unique (eng, control, quarter) triples → 2 silver reads,
    # not 3 (the 2nd row's Q1 hits the cache)
    assert silver_reader.read.call_count == 2


def test_re_fact_check_returns_zero_when_no_rows_match(
    capsys: pytest.CaptureFixture,
) -> None:
    """When no gold rows exist for the given (engagement,
    prompt_version), return early without calling silver."""
    update_capture: list[tuple[str, dict]] = []
    factory = _build_conn_factory(select_rows=[], update_capture=update_capture)
    silver_reader = MagicMock()

    result = re_fact_check(
        engagement_id="missing-engagement",
        prompt_version="v1.0",
        silver_reader=silver_reader,
        fact_checker=FactChecker(),
        conn_factory=factory,
        dry_run=False,
    )

    assert result == (0, 0, 0)
    silver_reader.read.assert_not_called()
    assert update_capture == []
