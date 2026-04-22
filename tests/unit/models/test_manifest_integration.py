"""Integration tests — manifest.yaml + full pipeline across all 20 scenarios.

Asserts that the 20-scenario manifest loads cleanly, covers the
required distribution, and can be driven through the Task-1 →
Task-2 → Task-3 pipeline without errors.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from agentic_audit.generator import populate_workbook, render_toc_sheet
from agentic_audit.models import ScenarioSpec, load_manifest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST_PATH = _REPO_ROOT / "eval" / "gold_scenarios" / "manifest.yaml"
_PLACEHOLDER_RE = re.compile(r"^<[a-z_0-9]+>$")


# ── Manifest shape ───────────────────────────────────────────────────


def test_manifest_has_exactly_20_scenarios() -> None:
    specs = load_manifest(_MANIFEST_PATH)
    assert len(specs) == 20


def test_scenario_ids_are_unique() -> None:
    specs = load_manifest(_MANIFEST_PATH)
    ids = [s.scenario_id for s in specs]
    assert len(set(ids)) == 20, f"Duplicate scenario_ids detected: {ids}"


def test_seeds_are_unique() -> None:
    specs = load_manifest(_MANIFEST_PATH)
    seeds = [s.seed for s in specs]
    assert len(set(seeds)) == 20, f"Duplicate seeds detected: {seeds}"


# ── Distribution ─────────────────────────────────────────────────────


def test_outcome_distribution_is_10_10() -> None:
    specs = load_manifest(_MANIFEST_PATH)
    outcomes = Counter(s.expected_outcome for s in specs)
    assert outcomes["pass"] == 10
    assert outcomes["exception"] == 10


def test_control_distribution_is_10_10() -> None:
    specs = load_manifest(_MANIFEST_PATH)
    controls = Counter(s.control_id for s in specs)
    assert controls["DC-9"] == 10
    assert controls["DC-2"] == 10


def test_quarter_distribution_is_10_10() -> None:
    specs = load_manifest(_MANIFEST_PATH)
    quarters = Counter(s.quarter for s in specs)
    assert quarters["Q1"] == 10
    assert quarters["Q3"] == 10


def test_every_exception_type_covered() -> None:
    """Every non-'none' exception_type Literal must have at least one scenario."""
    specs = load_manifest(_MANIFEST_PATH)
    exception_types = {s.exception_type for s in specs if s.exception_type != "none"}
    expected = {
        "signoff_missing",
        "figure_mismatch",
        "billing_rate_change_without_amendment",
        "variance_above_threshold_no_explanation",
        "variance_explanation_inadequate",
        "boundary_edge_case",
    }
    assert expected.issubset(exception_types), (
        f"Missing exception types: {expected - exception_types}"
    )


# ── Spec validity (already enforced by load_manifest, but assert loudly) ──


def test_all_specs_are_valid_scenariospec() -> None:
    specs = load_manifest(_MANIFEST_PATH)
    assert all(isinstance(s, ScenarioSpec) for s in specs)


def test_pass_scenarios_have_exception_type_none() -> None:
    specs = load_manifest(_MANIFEST_PATH)
    for s in specs:
        if s.expected_outcome == "pass":
            assert s.exception_type == "none", (
                f"{s.scenario_id}: pass outcome must have exception_type='none'"
            )


def test_exception_scenarios_have_specific_type() -> None:
    specs = load_manifest(_MANIFEST_PATH)
    for s in specs:
        if s.expected_outcome == "exception":
            assert s.exception_type != "none", (
                f"{s.scenario_id}: exception outcome must not have exception_type='none'"
            )


# ── Full pipeline (Task 1 → 2 → 3) ──────────────────────────────────


def test_all_scenarios_render_without_error() -> None:
    """Every scenario must produce a Task-2 workbook without raising."""
    specs = load_manifest(_MANIFEST_PATH)
    for spec in specs:
        wb = render_toc_sheet(spec)
        assert wb is not None, f"render_toc_sheet failed for {spec.scenario_id}"


def test_all_scenarios_populate_without_error() -> None:
    """Every scenario must survive the full pipeline without raising."""
    specs = load_manifest(_MANIFEST_PATH)
    for spec in specs:
        wb = populate_workbook(render_toc_sheet(spec), spec)
        assert wb is not None, f"populate_workbook failed for {spec.scenario_id}"


def test_no_placeholders_remain_in_any_scenario() -> None:
    """After populate, no cell must still contain a <placeholder>."""
    specs = load_manifest(_MANIFEST_PATH)
    for spec in specs:
        wb = populate_workbook(render_toc_sheet(spec), spec)
        ws = wb.active
        assert ws is not None
        for row in ws.iter_rows(values_only=True):
            for cell in row:
                if isinstance(cell, str):
                    assert not _PLACEHOLDER_RE.match(cell), (
                        f"{spec.scenario_id}: placeholder {cell!r} remains"
                    )


# ── Outcome-aware cell content ───────────────────────────────────────


def test_pass_scenarios_show_effective_conclusion() -> None:
    specs = [s for s in load_manifest(_MANIFEST_PATH) if s.expected_outcome == "pass"]
    for spec in specs:
        wb = populate_workbook(render_toc_sheet(spec), spec)
        ws = wb.active
        assert ws is not None
        effectiveness_values = {
            ws.cell(row=r, column=c).value
            for r in range(1, ws.max_row + 1)
            for c in range(1, 15)
            if ws.cell(row=r, column=c).value in {"Effective", "Not effective"}
        }
        assert effectiveness_values == {"Effective"}, (
            f"{spec.scenario_id}: pass scenario has unexpected effectiveness value"
        )


def test_exception_scenarios_show_not_effective_conclusion() -> None:
    specs = [s for s in load_manifest(_MANIFEST_PATH) if s.expected_outcome == "exception"]
    for spec in specs:
        wb = populate_workbook(render_toc_sheet(spec), spec)
        ws = wb.active
        assert ws is not None
        effectiveness_values = {
            ws.cell(row=r, column=c).value
            for r in range(1, ws.max_row + 1)
            for c in range(1, 15)
            if ws.cell(row=r, column=c).value in {"Effective", "Not effective"}
        }
        assert effectiveness_values == {"Not effective"}, (
            f"{spec.scenario_id}: exception scenario has unexpected effectiveness value"
        )


# ── Seed reproducibility across the full corpus ──────────────────────


def test_full_corpus_is_deterministic_across_runs() -> None:
    """Re-running the pipeline for all 20 scenarios must be byte-identical."""
    specs = load_manifest(_MANIFEST_PATH)

    def _dump(spec: ScenarioSpec) -> list[tuple[int, int, object]]:
        wb = populate_workbook(render_toc_sheet(spec), spec)
        ws = wb.active
        assert ws is not None
        return [
            (cell.row, cell.column, cell.value)
            for row in ws.iter_rows()
            for cell in row
            if cell.value is not None
        ]

    # Two full passes over the 20 scenarios — every pair must match
    for spec in specs:
        assert _dump(spec) == _dump(spec), (
            f"{spec.scenario_id}: non-deterministic across repeat renders"
        )


# ── Scenario-ID naming convention ───────────────────────────────────


def test_scenario_id_encodes_control_id() -> None:
    """scenario_id's dcN substring must match control_id.lower().replace('-', '')."""
    specs = load_manifest(_MANIFEST_PATH)
    for s in specs:
        expected_slug = s.control_id.lower().replace("-", "")
        assert expected_slug in s.scenario_id, (
            f"{s.scenario_id} does not encode control_id={s.control_id!r}"
        )


def test_scenario_id_encodes_quarter() -> None:
    specs = load_manifest(_MANIFEST_PATH)
    for s in specs:
        expected_slug = s.quarter.lower()
        assert s.scenario_id.startswith(expected_slug), (
            f"{s.scenario_id} does not start with {expected_slug!r}"
        )


def test_scenario_id_encodes_outcome() -> None:
    specs = load_manifest(_MANIFEST_PATH)
    for s in specs:
        assert s.expected_outcome in s.scenario_id, (
            f"{s.scenario_id} does not encode outcome={s.expected_outcome!r}"
        )
