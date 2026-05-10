"""Unit tests for ``scripts/run_layer2.py``.

The live-warehouse runner is not exercised in CI (no warehouse / Azure
OpenAI creds); these tests verify the sweep loop wiring with mocked
silver_reader / generator / fact_checker / gold_writer:

- 32 generate calls + 32 write_narrative calls per full sweep
- Silver caching: 8 read calls, not 32 (one per (engagement, control, quarter))
- Per-combination errors are caught and the sweep continues
- ``--dry-run`` skips LLM and writer entirely
- Summary print contains the canonical ``✓ N narratives ... pass rate K/N`` shape
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# `scripts/` isn't a package on PYTHONPATH by default; add it explicitly.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from run_layer2 import (  # type: ignore[import-not-found]  # noqa: E402
    _build_cost_telemetry,
    main,
    run_sweep,
)

from agentic_audit.models.evidence import (  # noqa: E402
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)
from agentic_audit.models.narrative import (  # noqa: E402
    AttributeNarrative,
    FactCheckResult,
)
from agentic_audit.models.telemetry import (  # noqa: E402
    CallUsage,
    UsageRecorder,
)

UTC_TS = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)


def _fake_evidence(control_id: str, quarter: str) -> ExtractedEvidence:
    ids = ["A", "B", "C", "D", "E", "F"] if control_id == "DC-9" else ["A", "B", "C", "D"]
    attrs = [
        AttributeCheck(
            control_id=control_id,  # type: ignore[arg-type]
            attribute_id=a,  # type: ignore[arg-type]
            status="pass",
            evidence_cell_refs=[f"{control_id.replace('-', '')}_WP!{a}1"],
            extracted_value={"sample": f"val-{a}"},
            notes=f"check {a}",
        )
        for a in ids
    ]
    return ExtractedEvidence(
        engagement_id="alpha-pension-fund-2025",
        control_id=control_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        run_id="01J0F7M5XQXM2QYAY8X8X8X8X8",
        extraction_timestamp=UTC_TS,
        preparer=SignOff(initials="AB", role="preparer", date=UTC_TS),
        reviewer=SignOff(initials="CD", role="reviewer", date=UTC_TS),
        attributes=attrs,
        source_bronze_file_hash="a" * 64,
        source_path=f"/bronze/dc{control_id.split('-')[1]}_{quarter}_ref.xlsx",
    )


def _fake_narrative(
    *,
    engagement_id: str,
    control_id: str,
    quarter: str,
    attribute_id: str,
    prompt_version: str = "v1.0",
    generation_run_id: str = "RUN_FAKE",
) -> AttributeNarrative:
    return AttributeNarrative(
        engagement_id=engagement_id,
        control_id=control_id,  # type: ignore[arg-type]
        attribute_id=attribute_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        source_evidence_id=f"{engagement_id}|{control_id}|{quarter}|{attribute_id}",
        narrative_text=f"Generated narrative for {control_id}.{attribute_id} {quarter}.",
        cited_fields=[f"{control_id.replace('-', '')}_WP!{attribute_id}1"],
        word_count=8,
        prompt_version=prompt_version,
        model_deployment="gpt-4o",
        generation_run_id=generation_run_id,
        generated_at=UTC_TS,
    )


# ---------- run_sweep — happy path ----------------------------------------


def test_run_sweep_full_engagement_writes_32_rows(capsys: pytest.CaptureFixture) -> None:
    """Full sweep over the 32 narratable combinations:
    32 generate calls, 32 write_narrative calls, 8 silver reads
    (one per (engagement, control, quarter) — silver caching)."""
    silver_reader = MagicMock()
    silver_reader.read.side_effect = lambda eng, c, q: _fake_evidence(c, q)

    generator = MagicMock()
    generator.generate.side_effect = lambda attribute, evidence, *, generation_run_id: (
        _fake_narrative(
            engagement_id=evidence.engagement_id,
            control_id=evidence.control_id,
            quarter=evidence.quarter,
            attribute_id=attribute,
            generation_run_id=generation_run_id,
        )
    )

    fact_checker = MagicMock()
    fact_checker.check.return_value = FactCheckResult(passed=True, issues=[])

    gold_writer = MagicMock()

    n_total, n_passed, _started_at, _completed_at = run_sweep(
        engagement_id="alpha-pension-fund-2025",
        prompt_version="v1.0",
        silver_reader=silver_reader,
        generator=generator,
        fact_checker=fact_checker,
        gold_writer=gold_writer,
        generation_run_id="RUN_TEST",
    )

    assert n_total == 32
    assert n_passed == 32
    # 32 generate calls
    assert generator.generate.call_count == 32
    # 32 write_narrative calls
    assert gold_writer.write_narrative.call_count == 32
    # Silver caching — 8 reads, not 32 (one per (engagement, control, quarter))
    assert silver_reader.read.call_count == 8


def test_run_sweep_propagates_fact_check_to_written_record() -> None:
    """The verdict from FactChecker.check() is inlined onto the
    AttributeNarrative before write — both for passing and failing
    cases."""
    silver_reader = MagicMock()
    silver_reader.read.side_effect = lambda eng, c, q: _fake_evidence(c, q)

    generator = MagicMock()
    generator.generate.side_effect = lambda attribute, evidence, *, generation_run_id: (
        _fake_narrative(
            engagement_id=evidence.engagement_id,
            control_id=evidence.control_id,
            quarter=evidence.quarter,
            attribute_id=attribute,
            generation_run_id=generation_run_id,
        )
    )

    fact_checker = MagicMock()
    # Alternate pass/fail per call — first passes, second fails, etc.
    fact_checker.check.side_effect = [
        FactCheckResult(passed=(i % 2 == 0), issues=[] if i % 2 == 0 else ["bad: x"])
        for i in range(32)
    ]

    gold_writer = MagicMock()

    _n_total, n_passed, _started_at, _completed_at = run_sweep(
        engagement_id="alpha-pension-fund-2025",
        prompt_version="v1.0",
        silver_reader=silver_reader,
        generator=generator,
        fact_checker=fact_checker,
        gold_writer=gold_writer,
        generation_run_id="RUN_TEST",
    )

    # 16 pass, 16 fail (even-indexed pass, odd-indexed fail)
    assert n_passed == 16
    # The records written carry the verdict — at least the first
    # written record should reflect passed=True.
    first_written_record = gold_writer.write_narrative.call_args_list[0][0][0]
    assert first_written_record.fact_check_passed is True
    assert first_written_record.fact_check_issues == []
    # Second written record should reflect passed=False with issues.
    second_written_record = gold_writer.write_narrative.call_args_list[1][0][0]
    assert second_written_record.fact_check_passed is False
    assert second_written_record.fact_check_issues == ["bad: x"]


# ---------- run_sweep — best-effort error handling ------------------------


def test_run_sweep_continues_on_single_combination_failure() -> None:
    """One combination raises mid-sweep; the remaining 31 still get
    written. Summary reflects 31 succeeded, not 32."""
    silver_reader = MagicMock()
    silver_reader.read.side_effect = lambda eng, c, q: _fake_evidence(c, q)

    call_count = {"n": 0}

    def generate_with_one_failure(attribute, evidence, *, generation_run_id):
        call_count["n"] += 1
        if call_count["n"] == 5:  # arbitrary middle of sweep
            raise RuntimeError("simulated LLM throttle on combination 5")
        return _fake_narrative(
            engagement_id=evidence.engagement_id,
            control_id=evidence.control_id,
            quarter=evidence.quarter,
            attribute_id=attribute,
            generation_run_id=generation_run_id,
        )

    generator = MagicMock()
    generator.generate.side_effect = generate_with_one_failure

    fact_checker = MagicMock()
    fact_checker.check.return_value = FactCheckResult(passed=True, issues=[])

    gold_writer = MagicMock()

    n_total, n_passed, _started_at, _completed_at = run_sweep(
        engagement_id="alpha-pension-fund-2025",
        prompt_version="v1.0",
        silver_reader=silver_reader,
        generator=generator,
        fact_checker=fact_checker,
        gold_writer=gold_writer,
        generation_run_id="RUN_TEST",
    )

    # 31 of 32 succeeded; the failed one is not in the totals
    assert n_total == 31
    assert n_passed == 31
    assert gold_writer.write_narrative.call_count == 31
    # generate was called 32 times (one of which raised)
    assert generator.generate.call_count == 32


# ---------- main — argparse + dry-run -------------------------------------


def test_main_dry_run_prints_32_combos_and_exits_zero(
    capsys: pytest.CaptureFixture,
) -> None:
    """``--dry-run`` skips LLM/writer and just prints the 32 combos
    + a summary. No env vars required, no warehouse touched."""
    rc = main(["--dry-run"])
    assert rc == 0
    captured = capsys.readouterr()
    # The 32 combos, one per line, plus the summary line.
    combo_lines = [
        line
        for line in captured.out.splitlines()
        if line.strip().startswith("alpha-pension-fund-2025")
    ]
    assert len(combo_lines) == 32
    assert "✓ dry run: 32 combinations" in captured.out


def test_main_dry_run_respects_engagement_arg(
    capsys: pytest.CaptureFixture,
) -> None:
    """``--engagement-id`` propagates into every printed line."""
    rc = main(["--dry-run", "--engagement-id", "bravo-2026"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "bravo-2026" in captured.out
    # And the default engagement should NOT appear
    assert "alpha-pension-fund-2025" not in captured.out


# ---------- run_sweep — timing window (cost telemetry follow-up #1) -------


def test_run_sweep_returns_started_and_completed_timestamps_in_order() -> None:
    """The sweep must return started_at <= completed_at; the caller
    derives latency_ms from this window."""
    silver_reader = MagicMock()
    silver_reader.read.side_effect = lambda eng, c, q: _fake_evidence(c, q)

    generator = MagicMock()
    generator.generate.side_effect = lambda attribute, evidence, *, generation_run_id: (
        _fake_narrative(
            engagement_id=evidence.engagement_id,
            control_id=evidence.control_id,
            quarter=evidence.quarter,
            attribute_id=attribute,
            generation_run_id=generation_run_id,
        )
    )

    fact_checker = MagicMock()
    fact_checker.check.return_value = FactCheckResult(passed=True, issues=[])
    gold_writer = MagicMock()

    _, _, started_at, completed_at = run_sweep(
        engagement_id="alpha-pension-fund-2025",
        prompt_version="v1.0",
        silver_reader=silver_reader,
        generator=generator,
        fact_checker=fact_checker,
        gold_writer=gold_writer,
        generation_run_id="RUN_TEST",
    )

    assert started_at <= completed_at
    # Both timestamps must be tz-aware UTC (CostTelemetry needs aware datetimes).
    assert started_at.tzinfo is not None
    assert completed_at.tzinfo is not None


# ---------- _build_cost_telemetry helper -----------------------------------


def test_build_cost_telemetry_known_deployment_computes_cost_usd() -> None:
    """For the gpt-4o deployment (in MODEL_PRICING_USD_PER_1K), the
    helper must populate cost_usd from the running totals."""
    recorder = UsageRecorder()
    recorder.record(CallUsage(prompt_tokens=10_000, completion_tokens=4_000))
    started = UTC_TS
    completed = datetime(2026, 5, 6, 12, 2, 0, tzinfo=UTC)  # +2 minutes

    telemetry = _build_cost_telemetry(
        agent_run_id="RUN_KNOWN",
        recorder=recorder,
        deployment="gpt-4o",
        started_at=started,
        completed_at=completed,
    )

    assert telemetry.agent_run_id == "RUN_KNOWN"
    assert telemetry.input_tokens == 10_000
    assert telemetry.output_tokens == 4_000
    assert telemetry.total_tokens == 14_000
    assert telemetry.latency_ms == 120_000
    # gpt-4o: 10k * 0.0025/1k + 4k * 0.0100/1k = 0.025 + 0.040 = 0.065
    assert telemetry.cost_usd == pytest.approx(0.065)
    assert telemetry.model_version == "gpt-4o"


def test_build_cost_telemetry_unknown_deployment_yields_none_cost(caplog) -> None:
    """An unknown deployment → cost_usd is None and a WARN is logged
    so the operator notices the price-table gap."""
    import logging

    recorder = UsageRecorder()
    recorder.record(CallUsage(prompt_tokens=100, completion_tokens=50))

    with caplog.at_level(logging.WARNING):
        telemetry = _build_cost_telemetry(
            agent_run_id="RUN_UNKNOWN",
            recorder=recorder,
            deployment="gpt-7-future",
            started_at=UTC_TS,
            completed_at=UTC_TS,
        )

    assert telemetry.cost_usd is None
    # Token + latency data still persisted — a NULL cost is better than a dropped row.
    assert telemetry.total_tokens == 150
    assert telemetry.latency_ms == 0
    # WARN message must name the deployment so the operator knows what to fix.
    warn_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("gpt-7-future" in m for m in warn_messages)
