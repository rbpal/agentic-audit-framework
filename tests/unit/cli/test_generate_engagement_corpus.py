"""Tests for ``generate_engagement_corpus`` — the v2 CLI orchestrator."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_audit.cli.generate_gold import generate_engagement_corpus
from agentic_audit.models.engagement_gold_answer import load_engagement_gold_answer

_REPO_ROOT = Path(__file__).resolve().parents[3]
_V2_MANIFEST = _REPO_ROOT / "eval" / "gold_scenarios" / "manifest.v2.yaml"


# ── File count + layout ──────────────────────────────────────────────


def test_emits_seventeen_files_total(tmp_path: Path) -> None:
    """1 TOC + 8 gold JSONs + 8 W/Ps = 17."""
    generate_engagement_corpus(_V2_MANIFEST, tmp_path)
    tocs_files = list((tmp_path / "tocs").iterdir())
    wp_files = list((tmp_path / "workpapers").iterdir())
    assert len(tocs_files) == 9  # 1 TOC xlsx + 8 gold JSONs
    assert len(wp_files) == 8


def test_emits_exactly_one_engagement_toc(tmp_path: Path) -> None:
    generate_engagement_corpus(_V2_MANIFEST, tmp_path)
    toc_xlsxs = list((tmp_path / "tocs").glob("*.xlsx"))
    assert len(toc_xlsxs) == 1
    assert toc_xlsxs[0].name == "engagement_toc_ref.xlsx"


def test_emits_eight_gold_jsons_with_expected_names(tmp_path: Path) -> None:
    generate_engagement_corpus(_V2_MANIFEST, tmp_path)
    json_files = sorted(p.name for p in (tmp_path / "tocs").glob("*.json"))
    assert json_files == [
        "dc2_Q1.json",
        "dc2_Q2.json",
        "dc2_Q3.json",
        "dc2_Q4.json",
        "dc9_Q1.json",
        "dc9_Q2.json",
        "dc9_Q3.json",
        "dc9_Q4.json",
    ]


def test_emits_eight_workpapers_with_expected_names(tmp_path: Path) -> None:
    generate_engagement_corpus(_V2_MANIFEST, tmp_path)
    wp_files = sorted(p.name for p in (tmp_path / "workpapers").iterdir())
    assert wp_files == [
        "dc2_Q1_ref.xlsx",
        "dc2_Q2_ref.xlsx",
        "dc2_Q3_ref.xlsx",
        "dc2_Q4_ref.xlsx",
        "dc9_Q1_ref.xlsx",
        "dc9_Q2_ref.xlsx",
        "dc9_Q3_ref.xlsx",
        "dc9_Q4_ref.xlsx",
    ]


def test_returns_sorted_list_of_all_written_paths(tmp_path: Path) -> None:
    written = generate_engagement_corpus(_V2_MANIFEST, tmp_path)
    assert len(written) == 17
    assert written == sorted(written)


# ── Content — gold answers hydrate back to the right shape ──────────


def test_gold_json_round_trips_through_load(tmp_path: Path) -> None:
    generate_engagement_corpus(_V2_MANIFEST, tmp_path)
    # Q3 figure_mismatch under §5 plan
    ans = load_engagement_gold_answer(tmp_path / "tocs" / "dc9_Q3.json")
    assert ans.control_id == "DC-9"
    assert ans.quarter == "Q3"
    assert ans.defect == "dc9_figure_mismatch"
    assert ans.expected_attribute_results["DC-9.C"] == "fail"
    assert ans.expected_cross_file_contradiction is not None


# ── Idempotency / determinism ───────────────────────────────────────


def test_regenerating_produces_byte_identical_output(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    generate_engagement_corpus(_V2_MANIFEST, first)
    generate_engagement_corpus(_V2_MANIFEST, second)

    # Compare every file's bytes
    first_files = sorted(first.rglob("*"))
    second_files = sorted(second.rglob("*"))
    first_rel = [p.relative_to(first) for p in first_files if p.is_file()]
    second_rel = [p.relative_to(second) for p in second_files if p.is_file()]
    assert first_rel == second_rel

    for rel in first_rel:
        assert (first / rel).read_bytes() == (second / rel).read_bytes(), (
            f"{rel} differs between runs — determinism broken"
        )


def test_same_dir_overwrite_is_idempotent(tmp_path: Path) -> None:
    generate_engagement_corpus(_V2_MANIFEST, tmp_path)
    first_bytes = {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    generate_engagement_corpus(_V2_MANIFEST, tmp_path)
    second_bytes = {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    assert first_bytes == second_bytes


# ── Cleanup: stale artefacts from previous runs are removed ──────────


def test_clears_stale_v1_tocs_and_workpapers(tmp_path: Path) -> None:
    """A prior v1 run leaves scenario-scoped files — v2 regen must remove them."""
    # Stage stale v1 artefacts
    (tmp_path / "tocs").mkdir()
    (tmp_path / "tocs" / "q1_pass_dc9_01_ref.xlsx").write_bytes(b"stale v1 toc")
    (tmp_path / "tocs" / "q1_pass_dc9_01.json").write_text('{"scenario_id": "stale"}')
    (tmp_path / "workpapers" / "q1_pass_dc9_01").mkdir(parents=True)
    (tmp_path / "workpapers" / "q1_pass_dc9_01" / "billing_calc.xlsx").write_bytes(b"stale v1 wp")

    generate_engagement_corpus(_V2_MANIFEST, tmp_path)

    # Stale files gone
    assert not (tmp_path / "tocs" / "q1_pass_dc9_01_ref.xlsx").exists()
    assert not (tmp_path / "tocs" / "q1_pass_dc9_01.json").exists()
    assert not (tmp_path / "workpapers" / "q1_pass_dc9_01").exists()


# ── Error paths ─────────────────────────────────────────────────────


def test_raises_on_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="manifest not found"):
        generate_engagement_corpus(tmp_path / "nope.yaml", tmp_path)


def test_creates_corpus_root_if_absent(tmp_path: Path) -> None:
    nested = tmp_path / "fresh" / "engagement"
    generate_engagement_corpus(_V2_MANIFEST, nested)
    assert (nested / "tocs").is_dir()
    assert (nested / "workpapers").is_dir()
    assert len(list((nested / "workpapers").iterdir())) == 8
