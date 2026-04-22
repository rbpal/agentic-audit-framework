"""Cross-consistency tests — gold-answer vs rendered workbook.

Guarantees that for every scenario in manifest.yaml, the attribute
flagged "fail" in the GoldAnswer matches the position of the "X"
tickmark in the Task-3-populated workbook. If this test ever fails,
it means fake_data and gold_answer drifted — fix the shared mapping.
"""

from __future__ import annotations

from pathlib import Path

from agentic_audit.generator import populate_workbook, render_toc_sheet
from agentic_audit.models import (
    GoldAnswer,
    build_gold_answer,
    gold_answer_to_json,
    load_gold_answer,
    load_manifest,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST_PATH = _REPO_ROOT / "eval" / "gold_scenarios" / "manifest.yaml"


def test_every_scenario_produces_valid_gold_answer() -> None:
    """For every spec in the 20-scenario manifest, build_gold_answer succeeds."""
    specs = load_manifest(_MANIFEST_PATH)
    assert len(specs) == 20
    for spec in specs:
        gold = build_gold_answer(spec)
        assert gold.scenario_id == spec.scenario_id
        assert gold.control_id == spec.control_id
        assert gold.expected_outcome == spec.expected_outcome


def test_gold_answer_fail_matches_workbook_x_tickmark() -> None:
    """For each scenario, the attribute marked 'fail' in the gold JSON must
    match the column where the workbook has an 'X' tickmark (if any).
    """
    specs = load_manifest(_MANIFEST_PATH)
    for spec in specs:
        gold = build_gold_answer(spec)
        wb = populate_workbook(render_toc_sheet(spec), spec)
        ws = wb.active
        assert ws is not None

        # Find sample-grid header row
        header_row = next(
            r for r in range(1, ws.max_row + 1) if ws.cell(row=r, column=2).value == "Sample item #"
        )
        # Check first sample row (all sample rows use same tickmark pattern)
        sample_row = header_row + 1

        for col in range(8, 14):  # H–M
            tickmark = ws.cell(row=sample_row, column=col).value
            if tickmark is None:
                continue
            letter = chr(ord("A") + col - 8)
            key = f"{spec.control_id}.{letter}"
            if key not in gold.expected_per_attribute_result:
                # Column exists in workbook but attribute not in gold (DC-2 has 4 attrs)
                continue

            if tickmark == "X":
                assert gold.expected_per_attribute_result[key] == "fail", (
                    f"{spec.scenario_id}: workbook has 'X' at {letter} "
                    f"but gold says '{gold.expected_per_attribute_result[key]}'"
                )
            elif tickmark == "a":
                assert gold.expected_per_attribute_result[key] == "pass", (
                    f"{spec.scenario_id}: workbook has 'a' at {letter} "
                    f"but gold says '{gold.expected_per_attribute_result[key]}'"
                )


def test_pass_scenarios_have_all_attributes_pass() -> None:
    specs = [s for s in load_manifest(_MANIFEST_PATH) if s.expected_outcome == "pass"]
    for spec in specs:
        gold = build_gold_answer(spec)
        assert all(v == "pass" for v in gold.expected_per_attribute_result.values()), (
            f"{spec.scenario_id}: pass scenario should have all attributes 'pass'"
        )


def test_exception_scenarios_have_exactly_one_attribute_fail() -> None:
    specs = [s for s in load_manifest(_MANIFEST_PATH) if s.expected_outcome == "exception"]
    for spec in specs:
        gold = build_gold_answer(spec)
        fail_count = sum(1 for v in gold.expected_per_attribute_result.values() if v == "fail")
        assert fail_count == 1, (
            f"{spec.scenario_id}: exception scenario should fail exactly one "
            f"attribute, got {fail_count}"
        )


def test_pass_scenarios_verdict_is_effective() -> None:
    specs = [s for s in load_manifest(_MANIFEST_PATH) if s.expected_outcome == "pass"]
    for spec in specs:
        gold = build_gold_answer(spec)
        assert gold.expected_final_verdict == "Effective"


def test_exception_scenarios_verdict_is_not_effective() -> None:
    specs = [s for s in load_manifest(_MANIFEST_PATH) if s.expected_outcome == "exception"]
    for spec in specs:
        gold = build_gold_answer(spec)
        assert gold.expected_final_verdict == "Not effective"


def test_exception_scenarios_have_narrative_keywords() -> None:
    specs = [s for s in load_manifest(_MANIFEST_PATH) if s.expected_outcome == "exception"]
    for spec in specs:
        gold = build_gold_answer(spec)
        assert len(gold.expected_exception_narrative_keywords) > 0, (
            f"{spec.scenario_id}: exception scenario must have narrative keywords"
        )


def test_gold_answer_json_roundtrip_for_all_scenarios(tmp_path: Path) -> None:
    """Every gold answer round-trips through JSON serialize + deserialize."""
    specs = load_manifest(_MANIFEST_PATH)
    for spec in specs:
        original = build_gold_answer(spec)
        text = gold_answer_to_json(original)

        # Write + reload via load_gold_answer
        path = tmp_path / f"{spec.scenario_id}.json"
        path.write_text(text)
        reloaded = load_gold_answer(path)

        assert isinstance(reloaded, GoldAnswer)
        assert reloaded == original
