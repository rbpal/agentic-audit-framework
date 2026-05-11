"""Unit tests for ``scripts/run_judge_sweep.py``.

The live-warehouse runner is not exercised in CI (no warehouse / Azure
OpenAI creds); these tests verify the sweep loop wiring with mocked
judge + writer:

- ``run_sweep`` iterates the injected ``narratives`` once
- One ``Judge.evaluate`` call per narrative
- One ``JudgeOutcomesWriter.write_judge_outcome`` call per narrative
- Per-narrative errors are caught and the sweep continues
- Summary tuple ``(n_total, verdict_counts, started_at, completed_at)``
  is returned, with ``verdict_counts`` always carrying the three fixed
  keys ``pass`` / ``fail`` / ``uncertain``

The first test below is the minimal red — it imports ``run_sweep``
from the script and exercises the empty-input path. Further tests
layer on real narratives, verdict aggregation, error handling, and
the ``_build_cost_telemetry`` / ``main`` plumbing once the orchestrator
skeleton lands.
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

from run_judge_sweep import (  # type: ignore[import-not-found]  # noqa: E402
    _load_gold_lookup_from_tocs,
    _new_run_id,
    main,
    run_sweep,
)

from agentic_audit.models.evidence import (  # noqa: E402
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)
from agentic_audit.models.judge import JudgeResponse  # noqa: E402
from agentic_audit.models.narrative import AttributeNarrative  # noqa: E402

UTC_TS = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


# ---------- fixtures -------------------------------------------------------


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
    engagement_id: str = "alpha-pension-fund-2025",
    control_id: str = "DC-9",
    quarter: str = "Q1",
    attribute_id: str = "A",
    fact_check_passed: bool = True,
) -> AttributeNarrative:
    return AttributeNarrative(
        engagement_id=engagement_id,
        control_id=control_id,  # type: ignore[arg-type]
        attribute_id=attribute_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        source_evidence_id=f"{engagement_id}|{control_id}|{quarter}|{attribute_id}",
        narrative_text=f"Narrative for {control_id}.{attribute_id} {quarter}.",
        cited_fields=[f"{control_id.replace('-', '')}_WP!{attribute_id}1"],
        word_count=6,
        prompt_version="v1.0",
        model_deployment="gpt-4o",
        generation_run_id="GEN_RUN_FAKE",
        generated_at=UTC_TS,
        fact_check_passed=fact_check_passed,
        fact_check_issues=[],
    )


# ---------- run_sweep — empty input ----------------------------------------


def test_run_sweep_zero_narratives_returns_empty_counts() -> None:
    """Empty ``narratives`` → no judge calls, no writes, zero counts.

    Started / completed timestamps are still set so the cost-telemetry
    summary row has a real wall-clock window even on an empty sweep.
    """
    silver_reader = MagicMock()
    judge = MagicMock()
    writer = MagicMock()

    n_total, verdict_counts, started_at, completed_at = run_sweep(
        narratives=[],
        silver_reader=silver_reader,
        judge=judge,
        writer=writer,
        gold_lookup={},
        judge_run_id="JUDGE_RUN_TEST",
    )

    assert n_total == 0
    assert verdict_counts == {"pass": 0, "fail": 0, "uncertain": 0}
    assert silver_reader.read.call_count == 0
    assert judge.evaluate.call_count == 0
    assert writer.write_judge_outcome.call_count == 0
    assert isinstance(started_at, datetime)
    assert isinstance(completed_at, datetime)
    assert completed_at >= started_at


# ---------- run_sweep — single narrative happy path ------------------------


def test_run_sweep_single_narrative_calls_silver_judge_and_writer() -> None:
    """One narrative → one silver read, one Judge.evaluate, one
    writer.write_judge_outcome. Verdict 'pass' increments
    verdict_counts['pass'] by 1.

    Pins the per-row contract: judge receives the narrative + evidence +
    gold_expected_verdict (from gold_lookup) + attribute_definition
    (formatted from ATTRIBUTE_DEFINITIONS_PER_CONTROL). Writer receives
    one outcome row.
    """
    narrative = _fake_narrative(control_id="DC-9", quarter="Q1", attribute_id="A")

    silver_reader = MagicMock()
    silver_reader.read.return_value = _fake_evidence("DC-9", "Q1")

    judge = MagicMock()
    judge.prompt_version = "judge_v1.0"
    judge.deployment = "gpt-4o"
    judge.evaluate.return_value = JudgeResponse(
        verdict="pass",
        confidence=0.9,
        reasoning="Evidence supports the claim.",
        cited_evidence_fields=["DC9_WP!A1"],
    )

    writer = MagicMock()
    gold_lookup = {("DC-9", "Q1", "A"): "pass"}

    n_total, counts, _, _ = run_sweep(
        narratives=[narrative],
        silver_reader=silver_reader,
        judge=judge,
        writer=writer,
        gold_lookup=gold_lookup,
        judge_run_id="JUDGE_RUN_TEST",
    )

    assert n_total == 1
    assert counts == {"pass": 1, "fail": 0, "uncertain": 0}

    silver_reader.read.assert_called_once_with("alpha-pension-fund-2025", "DC-9", "Q1")

    judge.evaluate.assert_called_once()
    kwargs = judge.evaluate.call_args.kwargs
    assert kwargs["gold_expected_verdict"] == "pass"
    assert kwargs["attribute_definition"] == ("DC-9.A — Preparer signed off on the Checklist")

    writer.write_judge_outcome.assert_called_once()


# ---------- run_sweep — writer row shape -----------------------------------


def test_run_sweep_writer_receives_full_judge_outcome_row() -> None:
    """Writer arg pins all 16 columns of ``gold.judge_outcomes``.

    Three pieces of denormalised state are folded into each row:

    1. **From the narrative** — engagement_id, control_id, attribute_id,
       quarter, narrative_run_id (= narrative.narrative_call_id when
       populated, falling back to narrative.generation_run_id for
       historical v1.0 rows that pre-date Step 5 follow-up #5),
       fact_check_verdict (translated from narrative.fact_check_passed
       bool: True→"pass", False→"fail").
    2. **From the judge** — judge_verdict, judge_confidence,
       judge_reasoning, cited_evidence_fields, plus the judge's own
       prompt_version and model_deployment (NOT the narrative's; the
       column documents which judge prompt + model produced THIS
       verdict).
    3. **From the sweep** — judge_run_id (constant per sweep),
       evaluated_at (UTC at judge call return time), judge_status
       ("ok" for a happy-path verdict).
    4. **From the gold lookup** — gold_expected_verdict.
    """
    narrative = _fake_narrative(
        control_id="DC-9", quarter="Q1", attribute_id="A", fact_check_passed=False
    )

    silver_reader = MagicMock()
    silver_reader.read.return_value = _fake_evidence("DC-9", "Q1")

    judge = MagicMock()
    judge.prompt_version = "judge_v1.0"
    judge.deployment = "gpt-4o"
    judge.evaluate.return_value = JudgeResponse(
        verdict="fail",
        confidence=0.85,
        reasoning="Reviewer cell blank; claim unsupported.",
        cited_evidence_fields=["DC9_WP!B5"],
    )

    writer = MagicMock()
    gold_lookup = {("DC-9", "Q1", "A"): "pass"}  # judge=fail, gold=pass — divergence

    run_sweep(
        narratives=[narrative],
        silver_reader=silver_reader,
        judge=judge,
        writer=writer,
        gold_lookup=gold_lookup,
        judge_run_id="JUDGE_RUN_TEST",
    )

    writer.write_judge_outcome.assert_called_once()
    outcome = writer.write_judge_outcome.call_args.args[0]

    # 1. Carried from the narrative
    assert outcome.engagement_id == "alpha-pension-fund-2025"
    assert outcome.control_id == "DC-9"
    assert outcome.attribute_id == "A"
    assert outcome.quarter == "Q1"
    assert outcome.narrative_run_id == narrative.generation_run_id
    assert outcome.fact_check_verdict == "fail"  # narrative.fact_check_passed=False

    # 2. Carried from the judge
    assert outcome.judge_verdict == "fail"
    assert outcome.judge_confidence == 0.85
    assert outcome.judge_reasoning == "Reviewer cell blank; claim unsupported."
    assert outcome.cited_evidence_fields == ["DC9_WP!B5"]
    assert outcome.prompt_version == "judge_v1.0"
    assert outcome.model_deployment == "gpt-4o"

    # 3. Carried from the sweep
    assert outcome.judge_run_id == "JUDGE_RUN_TEST"
    assert outcome.judge_status == "ok"
    assert isinstance(outcome.evaluated_at, datetime)

    # 4. Carried from the gold lookup
    assert outcome.gold_expected_verdict == "pass"


# ---------- run_sweep — narrative_call_id preference (follow-up #5) --------


def test_run_sweep_prefers_narrative_call_id_over_generation_run_id() -> None:
    """Step 5 follow-up #5: when the narrative carries a per-call
    ``narrative_call_id``, the judge sweep writes THAT to
    ``judge_outcomes.narrative_run_id`` — NOT the sweep-scoped
    ``generation_run_id``.

    This is what collapses the composite-key Cartesian product
    workaround in ``scripts/divergence_summary.sql`` Q2 down to a
    single-condition join for v1.1+ sweeps.
    """
    narrative = _fake_narrative()
    # Simulate a v1.1+ row that carries the per-call id
    narrative_with_call_id = narrative.model_copy(
        update={"narrative_call_id": "CALL_FAKE_DISTINCT"}
    )
    assert narrative_with_call_id.generation_run_id == "GEN_RUN_FAKE"
    assert narrative_with_call_id.narrative_call_id == "CALL_FAKE_DISTINCT"

    silver_reader = MagicMock()
    silver_reader.read.return_value = _fake_evidence("DC-9", "Q1")
    judge = MagicMock()
    judge.prompt_version = "judge_v1.0"
    judge.deployment = "gpt-4o"
    judge.evaluate.return_value = JudgeResponse(
        verdict="pass", confidence=0.9, reasoning="r", cited_evidence_fields=["x"]
    )
    writer = MagicMock()

    run_sweep(
        narratives=[narrative_with_call_id],
        silver_reader=silver_reader,
        judge=judge,
        writer=writer,
        gold_lookup={("DC-9", "Q1", "A"): "pass"},
        judge_run_id="JUDGE_RUN_TEST",
    )

    outcome = writer.write_judge_outcome.call_args.args[0]
    assert outcome.narrative_run_id == "CALL_FAKE_DISTINCT"


def test_run_sweep_falls_back_to_generation_run_id_when_call_id_null() -> None:
    """Historical v1.0 rows have ``narrative_call_id=None`` (pre-Step 5
    follow-up #5). The sweep must fall back to the sweep-scoped
    ``generation_run_id`` so old narratives round-trip cleanly.

    Without this fallback, re-running the judge against the v1.0
    baseline would raise (narrative_run_id is a required string in
    JudgeOutcomeRow).
    """
    # _fake_narrative leaves narrative_call_id at the model default (None)
    narrative = _fake_narrative()
    assert narrative.narrative_call_id is None

    silver_reader = MagicMock()
    silver_reader.read.return_value = _fake_evidence("DC-9", "Q1")
    judge = MagicMock()
    judge.prompt_version = "judge_v1.0"
    judge.deployment = "gpt-4o"
    judge.evaluate.return_value = JudgeResponse(
        verdict="pass", confidence=0.9, reasoning="r", cited_evidence_fields=["x"]
    )
    writer = MagicMock()

    run_sweep(
        narratives=[narrative],
        silver_reader=silver_reader,
        judge=judge,
        writer=writer,
        gold_lookup={("DC-9", "Q1", "A"): "pass"},
        judge_run_id="JUDGE_RUN_TEST",
    )

    outcome = writer.write_judge_outcome.call_args.args[0]
    assert outcome.narrative_run_id == narrative.generation_run_id
    assert outcome.narrative_run_id == "GEN_RUN_FAKE"


# ---------- run_sweep — silver caching invariant ---------------------------


def test_run_sweep_two_narratives_same_quarter_cache_silver_read() -> None:
    """Two narratives under the same ``(engagement, control, quarter)``
    triple → ``silver_reader.read`` called exactly once, evidence
    reused on the second call.

    Mirrors the 32-narratives / 8-reads invariant from ``run_layer2.py``.
    Without caching, a 32-narrative sweep would issue 32 silver reads;
    with caching, 8 (one per attribute group).
    """
    n1 = _fake_narrative(control_id="DC-9", quarter="Q1", attribute_id="A")
    n2 = _fake_narrative(control_id="DC-9", quarter="Q1", attribute_id="B")

    silver_reader = MagicMock()
    silver_reader.read.return_value = _fake_evidence("DC-9", "Q1")

    judge = MagicMock()
    judge.prompt_version = "judge_v1.0"
    judge.deployment = "gpt-4o"
    judge.evaluate.return_value = JudgeResponse(
        verdict="pass",
        confidence=0.9,
        reasoning="ok",
        cited_evidence_fields=["DC9_WP!A1"],
    )

    writer = MagicMock()
    gold_lookup = {
        ("DC-9", "Q1", "A"): "pass",
        ("DC-9", "Q1", "B"): "pass",
    }

    n_total, _, _, _ = run_sweep(
        narratives=[n1, n2],
        silver_reader=silver_reader,
        judge=judge,
        writer=writer,
        gold_lookup=gold_lookup,
        judge_run_id="JUDGE_RUN_TEST",
    )

    assert n_total == 2
    silver_reader.read.assert_called_once_with("alpha-pension-fund-2025", "DC-9", "Q1")
    assert judge.evaluate.call_count == 2
    assert writer.write_judge_outcome.call_count == 2


# ---------- run_sweep — verdict aggregation across pass/fail/uncertain -----


def test_run_sweep_aggregates_three_verdicts_into_counts() -> None:
    """Three narratives, one of each verdict → counts split 1/1/1.

    Pins the invariant that ``verdict_counts`` carries the THREE keys
    pass/fail/uncertain and is incremented by exactly one per row,
    regardless of which verdict the judge returns.
    """
    narratives = [
        _fake_narrative(control_id="DC-9", quarter="Q1", attribute_id="A"),
        _fake_narrative(control_id="DC-9", quarter="Q1", attribute_id="B"),
        _fake_narrative(control_id="DC-9", quarter="Q1", attribute_id="C"),
    ]

    silver_reader = MagicMock()
    silver_reader.read.return_value = _fake_evidence("DC-9", "Q1")

    judge = MagicMock()
    judge.prompt_version = "judge_v1.0"
    judge.deployment = "gpt-4o"
    judge.evaluate.side_effect = [
        JudgeResponse(
            verdict="pass",
            confidence=0.9,
            reasoning="ok",
            cited_evidence_fields=["X"],
        ),
        JudgeResponse(
            verdict="fail",
            confidence=0.85,
            reasoning="bad",
            cited_evidence_fields=["Y"],
        ),
        JudgeResponse(
            verdict="uncertain",
            confidence=0.3,
            reasoning="evidence is silent on this point",
            cited_evidence_fields=[],  # uncertain exempt from Decision Rule 1
        ),
    ]

    writer = MagicMock()
    gold_lookup = {
        ("DC-9", "Q1", "A"): "pass",
        ("DC-9", "Q1", "B"): "pass",
        ("DC-9", "Q1", "C"): "pass",
    }

    n_total, counts, _, _ = run_sweep(
        narratives=narratives,
        silver_reader=silver_reader,
        judge=judge,
        writer=writer,
        gold_lookup=gold_lookup,
        judge_run_id="JUDGE_RUN_TEST",
    )

    assert n_total == 3
    assert counts == {"pass": 1, "fail": 1, "uncertain": 1}
    assert writer.write_judge_outcome.call_count == 3


# ---------- run_sweep — per-row error handling -----------------------------


def test_run_sweep_continues_after_per_row_exception() -> None:
    """A judge exception on one row does NOT kill the sweep.

    The failed row is not counted in ``n_total``, not written to the
    writer, and not added to ``verdict_counts``. The sweep continues
    to subsequent rows. The eval harness is observability, not a
    control-flow gate (task_03 design rationale).

    Two of three narratives succeed; the middle one raises.
    """
    narratives = [
        _fake_narrative(control_id="DC-9", quarter="Q1", attribute_id="A"),
        _fake_narrative(control_id="DC-9", quarter="Q1", attribute_id="B"),
        _fake_narrative(control_id="DC-9", quarter="Q1", attribute_id="C"),
    ]

    silver_reader = MagicMock()
    silver_reader.read.return_value = _fake_evidence("DC-9", "Q1")

    judge = MagicMock()
    judge.prompt_version = "judge_v1.0"
    judge.deployment = "gpt-4o"
    judge.evaluate.side_effect = [
        JudgeResponse(
            verdict="pass",
            confidence=0.9,
            reasoning="ok",
            cited_evidence_fields=["X"],
        ),
        RuntimeError("simulated transient auth / network failure"),
        JudgeResponse(
            verdict="fail",
            confidence=0.8,
            reasoning="bad",
            cited_evidence_fields=["Y"],
        ),
    ]

    writer = MagicMock()
    gold_lookup = {
        ("DC-9", "Q1", "A"): "pass",
        ("DC-9", "Q1", "B"): "pass",
        ("DC-9", "Q1", "C"): "fail",
    }

    n_total, counts, _, _ = run_sweep(
        narratives=narratives,
        silver_reader=silver_reader,
        judge=judge,
        writer=writer,
        gold_lookup=gold_lookup,
        judge_run_id="JUDGE_RUN_TEST",
    )

    assert n_total == 2  # not 3 — the failed row is not counted
    assert counts == {"pass": 1, "fail": 1, "uncertain": 0}
    assert judge.evaluate.call_count == 3  # all three rows were attempted
    assert writer.write_judge_outcome.call_count == 2  # only successes written


# ---------- _load_gold_lookup_from_tocs ------------------------------------


def test_load_gold_lookup_returns_per_attribute_verdicts() -> None:
    """Loads the 8 ToC JSONs at ``eval/gold_scenarios/tocs/*.json``
    and returns a flat dict keyed by ``(control_id, quarter, attribute_id)``
    with the verdict string from the ToC's
    ``expected_attribute_results``.

    Total = DC-2 (4 attrs) × 4 quarters + DC-9 (6 attrs) × 4 quarters
    = 16 + 24 = 40 entries.

    The Step 6 judge sweep uses this lookup to populate
    ``gold_expected_verdict`` on every ``JudgeOutcomeRow`` without
    having to re-read JSON files per narrative.
    """
    repo_root = Path(__file__).resolve().parents[3]
    toc_dir = repo_root / "eval" / "gold_scenarios" / "tocs"

    lookup = _load_gold_lookup_from_tocs(toc_dir)

    assert len(lookup) == 40  # 4×4 (DC-2) + 6×4 (DC-9)

    # Pinned verdicts — these are the corpus-baked defects from
    # step_01_synthetic_data.md §4.3 / §4.6.
    assert lookup[("DC-9", "Q1", "A")] == "pass"
    assert lookup[("DC-9", "Q3", "C")] == "fail"  # dc9_figure_mismatch
    assert lookup[("DC-9", "Q4", "D")] == "fail"  # dc9_rate_change_without_amendment
    assert lookup[("DC-2", "Q3", "C")] == "fail"  # dc2_variance_explanation_inadequate
    assert lookup[("DC-2", "Q4", "B")] == "fail"  # dc2_variance_no_explanation
    assert lookup[("DC-2", "Q1", "A")] == "pass"


def test_load_gold_lookup_keys_match_attributes_per_control() -> None:
    """Every (control, quarter, attribute) combination from
    ATTRIBUTES_PER_CONTROL appears in the lookup. No gaps."""
    from agentic_audit.models.evidence import ATTRIBUTES_PER_CONTROL

    repo_root = Path(__file__).resolve().parents[3]
    toc_dir = repo_root / "eval" / "gold_scenarios" / "tocs"

    lookup = _load_gold_lookup_from_tocs(toc_dir)

    for control_id, attrs in ATTRIBUTES_PER_CONTROL.items():
        for quarter in ("Q1", "Q2", "Q3", "Q4"):
            for attr in attrs:
                assert (control_id, quarter, attr) in lookup, (
                    f"missing gold verdict for ({control_id}, {quarter}, {attr})"
                )
                assert lookup[(control_id, quarter, attr)] in ("pass", "fail")


# ---------- _new_run_id ----------------------------------------------------


def test_new_run_id_is_32_hex_chars_upper() -> None:
    """Matches the generator's run id shape from run_layer2.py so
    cost telemetry queries can join cleanly across sweep families."""
    run_id = _new_run_id()
    assert len(run_id) == 32
    assert run_id == run_id.upper()
    assert all(c in "0123456789ABCDEF" for c in run_id)


def test_new_run_id_is_unique_across_calls() -> None:
    """Two consecutive calls must not collide — the run id is the
    primary key joining cost_telemetry to a specific sweep."""
    ids = {_new_run_id() for _ in range(100)}
    assert len(ids) == 100


# ---------- main --dry-run -------------------------------------------------


def test_main_dry_run_prints_lookup_summary_and_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main(['--dry-run'])`` loads the gold lookup, prints a
    one-line summary plus a preview of the 32 narratable combinations
    with their gold expected verdicts, and returns 0. No env-var
    auth, no LLM calls, no writes."""
    repo_root = Path(__file__).resolve().parents[3]
    toc_dir = str(repo_root / "eval" / "gold_scenarios" / "tocs")

    exit_code = main(["--dry-run", "--toc-dir", toc_dir])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "40 gold-verdict entries loaded" in out
    # 32 narratable combinations previewed — one line each
    assert "DC-2 Q1 A" in out
    assert "DC-9 Q3 C" in out
    # Layer-3 attributes excluded from the preview (B for DC-2, D for DC-9)
    assert "DC-2 Q1 B" not in out
    assert "DC-9 Q1 D" not in out
    assert "32 combinations would be evaluated" in out


def test_main_live_without_databricks_creds_exits_with_clear_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Live mode (no ``--dry-run``) requires DATABRICKS_HOST /
    DATABRICKS_TOKEN / DATABRICKS_SQL_WAREHOUSE_ID. Missing any of
    these triggers a clear stderr error + ``SystemExit(2)`` before
    any LLM or warehouse calls are attempted."""
    # Clear creds so the factory builder sees nothing
    for var in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_SQL_WAREHOUSE_ID"):
        monkeypatch.delenv(var, raising=False)

    repo_root = Path(__file__).resolve().parents[3]
    toc_dir = str(repo_root / "eval" / "gold_scenarios" / "tocs")

    with pytest.raises(SystemExit) as exc_info:
        main(["--toc-dir", toc_dir])

    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "DATABRICKS_HOST" in err
    assert "missing env vars" in err.lower()


def test_main_live_runs_full_pipeline_with_mocked_components(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Smoke test of the live wiring: when all external collaborators
    are mocked, ``main()`` orchestrates them end-to-end. Verifies that:

    - GoldNarrativesReader.iter_narratives is called with the engagement_id
    - run_sweep is invoked with one narrative -> one judge call + writer call
    - CostTelemetryWriter.write_cost_telemetry receives one row
    - stdout summary shows the expected verdict counts and cost numbers
    - Exit code is 0

    This is the integration boundary for cycle D — proves the live
    pipeline wires up correctly without needing live cloud creds.
    """
    import run_judge_sweep as mod  # type: ignore[import-not-found]

    # Build a complete narrative-shaped fixture
    fake_narrative = _fake_narrative(control_id="DC-9", quarter="Q1", attribute_id="A")

    fake_factory = MagicMock()
    mock_judge = MagicMock()
    mock_judge.deployment = "gpt-4o"
    mock_judge.prompt_version = "judge_v1.0"
    mock_judge.evaluate.return_value = JudgeResponse(
        verdict="pass",
        confidence=0.9,
        reasoning="evidence supports the claim",
        cited_evidence_fields=["DC9_WP!A1"],
    )

    mock_narratives_reader = MagicMock()
    mock_narratives_reader.iter_narratives.return_value = [fake_narrative]

    mock_silver_reader = MagicMock()
    mock_silver_reader.read.return_value = _fake_evidence("DC-9", "Q1")

    mock_writer = MagicMock()
    mock_cost_writer = MagicMock()

    # Replace factory + constructors with mocks
    monkeypatch.setattr(mod, "_build_warehouse_conn_factory", lambda: fake_factory)
    monkeypatch.setattr(
        mod.Judge,
        "from_env",
        classmethod(lambda cls, *, usage_recorder=None: mock_judge),
    )
    monkeypatch.setattr(mod, "SilverEvidenceReader", lambda *_a, **_kw: mock_silver_reader)
    monkeypatch.setattr(mod, "GoldNarrativesReader", lambda *_a, **_kw: mock_narratives_reader)
    monkeypatch.setattr(mod, "JudgeOutcomesWriter", lambda *_a, **_kw: mock_writer)
    monkeypatch.setattr(mod, "CostTelemetryWriter", lambda *_a, **_kw: mock_cost_writer)

    repo_root = Path(__file__).resolve().parents[3]
    toc_dir = str(repo_root / "eval" / "gold_scenarios" / "tocs")

    exit_code = main(["--toc-dir", toc_dir])

    assert exit_code == 0

    # Wiring assertions.
    # prompt_version defaults to v1.0 (Step 5 follow-up #4) — flag was
    # added so v1.1 re-baseline sweeps can be scoped explicitly.
    mock_narratives_reader.iter_narratives.assert_called_once_with(
        "alpha-pension-fund-2025", prompt_version="v1.0"
    )
    mock_judge.evaluate.assert_called_once()
    mock_writer.write_judge_outcome.assert_called_once()
    mock_cost_writer.write_cost_telemetry.assert_called_once()

    # Summary print
    out = capsys.readouterr().out
    assert "1 narratives evaluated" in out
    assert "pass/fail/uncertain = 1/0/0" in out
    assert "cost telemetry" in out
    assert "LLM calls" in out


def test_main_live_prompt_version_v1_1_threads_through_to_reader(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Step 5 follow-up #4: the ``--prompt-version`` CLI flag scopes
    the sweep to a specific narrative cohort in ``gold.narratives``.

    Without this wiring, a v1.1 re-baseline sweep would silently read
    the v1.0 baseline rows (the reader's default), and the judge
    sweep would re-judge v1.0 narratives instead of v1.1 ones.
    """
    import run_judge_sweep as mod  # type: ignore[import-not-found]

    fake_narrative = _fake_narrative(control_id="DC-9", quarter="Q1", attribute_id="A")
    # Override the prompt_version on the fixture so the assertion is unambiguous.
    fake_narrative_v1_1 = fake_narrative.model_copy(update={"prompt_version": "v1.1"})

    mock_judge = MagicMock()
    mock_judge.deployment = "gpt-4o"
    mock_judge.prompt_version = "judge_v1.0"
    mock_judge.evaluate.return_value = JudgeResponse(
        verdict="pass", confidence=0.9, reasoning="r", cited_evidence_fields=["x"]
    )

    mock_narratives_reader = MagicMock()
    mock_narratives_reader.iter_narratives.return_value = [fake_narrative_v1_1]

    mock_silver_reader = MagicMock()
    mock_silver_reader.read.return_value = _fake_evidence("DC-9", "Q1")

    mock_writer = MagicMock()
    mock_cost_writer = MagicMock()

    monkeypatch.setattr(mod, "_build_warehouse_conn_factory", lambda: MagicMock())
    monkeypatch.setattr(mod, "Judge", MagicMock(from_env=lambda **_kw: mock_judge))
    monkeypatch.setattr(mod, "SilverEvidenceReader", lambda *_a, **_kw: mock_silver_reader)
    monkeypatch.setattr(mod, "GoldNarrativesReader", lambda *_a, **_kw: mock_narratives_reader)
    monkeypatch.setattr(mod, "JudgeOutcomesWriter", lambda *_a, **_kw: mock_writer)
    monkeypatch.setattr(mod, "CostTelemetryWriter", lambda *_a, **_kw: mock_cost_writer)

    repo_root = Path(__file__).resolve().parents[3]
    toc_dir = str(repo_root / "eval" / "gold_scenarios" / "tocs")

    exit_code = main(["--toc-dir", toc_dir, "--prompt-version", "v1.1"])
    assert exit_code == 0

    # The wiring assertion: the reader was scoped to v1.1, not the default v1.0.
    mock_narratives_reader.iter_narratives.assert_called_once_with(
        "alpha-pension-fund-2025", prompt_version="v1.1"
    )

    # And the operator banner reflects both narrative + judge prompt versions
    # without hardcoding either.
    out = capsys.readouterr().out
    assert "narrative_prompt_version=v1.1" in out
    assert "judge_prompt_version=judge_v1.0" in out
