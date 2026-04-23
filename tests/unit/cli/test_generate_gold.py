"""Tests for ``agentic_audit.cli.generate_gold``.

Task 6 contract:
* CLI runs end-to-end and writes 20 .xlsx + 20 .json.
* Outputs are byte-identical across repeated runs (idempotency).
* Stale files that do not correspond to a current scenario are removed.
* Helper ``strip_workbook_metadata`` pins docprops to fixed values.

Task 7 will add the content-hash helper + broader determinism coverage.
"""

from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from agentic_audit.cli.generate_gold import (
    _FIXED_EPOCH,
    _ZIP_FIXED_DATE_TIME,
    generate_gold,
    main,
    strip_workbook_metadata,
    write_hash_manifest,
)
from agentic_audit.models import load_manifest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST_PATH = _REPO_ROOT / "eval" / "gold_scenarios" / "manifest.yaml"


# ── generate_gold: happy-path smoke tests ────────────────────────────


def test_generate_gold_writes_twenty_xlsx_and_twenty_json(tmp_path: Path) -> None:
    generate_gold(_MANIFEST_PATH, tmp_path)
    tocs = tmp_path / "tocs"

    xlsx_files = sorted(tocs.glob("*.xlsx"))
    json_files = sorted(tocs.glob("*.json"))
    assert len(xlsx_files) == 20
    assert len(json_files) == 20


def test_generate_gold_filenames_match_scenario_ids(tmp_path: Path) -> None:
    """xlsx files carry _ref suffix; json gold answers do not."""
    generate_gold(_MANIFEST_PATH, tmp_path)
    tocs = tmp_path / "tocs"

    specs = load_manifest(_MANIFEST_PATH)
    expected_ids = {spec.scenario_id for spec in specs}
    # xlsx stem is "<scenario_id>_ref" — strip suffix before comparing
    xlsx_scenario_ids = {p.stem.removesuffix("_ref") for p in tocs.glob("*.xlsx")}
    json_stems = {p.stem for p in tocs.glob("*.json")}

    assert xlsx_scenario_ids == expected_ids
    assert json_stems == expected_ids
    # Every xlsx must carry _ref — no bare <scenario_id>.xlsx allowed
    for p in tocs.glob("*.xlsx"):
        assert p.stem.endswith("_ref"), f"{p.name} missing _ref suffix"


def test_generate_gold_returns_sorted_paths(tmp_path: Path) -> None:
    written = generate_gold(_MANIFEST_PATH, tmp_path)
    assert written == sorted(written)
    # 20 TOC xlsx + 20 gold JSON + 10 billing-calc W/P (DC-9 scenarios)
    assert len(written) == 50
    # TOCs + JSONs live directly under tocs/; W/Ps under workpapers/<scenario_id>/
    for p in written:
        parent_name = p.parent.name
        grandparent = p.parent.parent.name if p.parent.parent.name else ""
        assert parent_name == "tocs" or grandparent == "workpapers", (
            f"{p} is not under tocs/ or workpapers/<scenario_id>/"
        )


# ── idempotency: byte-identical outputs across runs ──────────────────


def test_generate_gold_is_byte_idempotent(tmp_path: Path) -> None:
    """Two consecutive runs must produce byte-identical outputs — the
    whole point of Task 6's metadata + zip-timestamp stripping.
    """
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    generate_gold(_MANIFEST_PATH, first)
    generate_gold(_MANIFEST_PATH, second)

    first_files = sorted((first / "tocs").glob("*"))
    second_files = sorted((second / "tocs").glob("*"))
    assert [p.name for p in first_files] == [p.name for p in second_files]

    for a, b in zip(first_files, second_files, strict=True):
        assert a.read_bytes() == b.read_bytes(), (
            f"{a.name} differs between runs — determinism broken"
        )


def test_generate_gold_overwrite_same_dir_is_idempotent(tmp_path: Path) -> None:
    """Running twice into the SAME directory (clear + regen) must be stable."""
    generate_gold(_MANIFEST_PATH, tmp_path)
    tocs = tmp_path / "tocs"
    first_bytes = {p.name: p.read_bytes() for p in tocs.iterdir()}

    generate_gold(_MANIFEST_PATH, tmp_path)
    second_bytes = {p.name: p.read_bytes() for p in tocs.iterdir()}

    assert first_bytes == second_bytes


# ── stale-file cleanup ────────────────────────────────────────────────


def test_generate_gold_removes_stale_xlsx(tmp_path: Path) -> None:
    tocs = tmp_path / "tocs"
    tocs.mkdir()
    stale = tocs / "q9_stale_scenario.xlsx"
    stale.write_bytes(b"stale zip bytes")
    generate_gold(_MANIFEST_PATH, tmp_path)
    assert not stale.exists()


def test_generate_gold_removes_stale_json(tmp_path: Path) -> None:
    tocs = tmp_path / "tocs"
    tocs.mkdir()
    stale = tocs / "q9_stale_scenario.json"
    stale.write_text('{"scenario_id": "stale"}')
    generate_gold(_MANIFEST_PATH, tmp_path)
    assert not stale.exists()


def test_generate_gold_preserves_manifest_yaml(tmp_path: Path) -> None:
    """manifest.yaml lives at corpus root — clearing tocs/ must leave it alone."""
    manifest_copy = tmp_path / "manifest.yaml"
    manifest_copy.write_text(_MANIFEST_PATH.read_text())
    generate_gold(manifest_copy, tmp_path)
    assert manifest_copy.exists()


def test_generate_gold_does_not_touch_unrelated_siblings(tmp_path: Path) -> None:
    """Cleanup is scoped to ``tocs/`` and ``workpapers/`` — any other sibling
    file or directory at the corpus root must survive regeneration untouched.

    ``workpapers/`` itself IS wiped (Task 12 — all W/P content is derived
    from the manifest); the test above covers that case. This test pins
    down that *unrelated* sibs (README, custom fixtures, docs) stay.
    """
    readme = tmp_path / "README.txt"
    readme.write_text("custom contributor notes")

    custom_dir = tmp_path / "local_fixtures"
    custom_dir.mkdir()
    (custom_dir / "keep_me.txt").write_text("do not delete")

    generate_gold(_MANIFEST_PATH, tmp_path)

    assert readme.exists()
    assert readme.read_text() == "custom contributor notes"
    assert (custom_dir / "keep_me.txt").exists()


def test_generate_gold_wipes_and_rebuilds_workpapers_dir(tmp_path: Path) -> None:
    """Stale W/Ps must not linger — scenarios that no longer declare a
    workpaper must leave no orphan file behind. The entire workpapers/
    subtree is regenerated from the manifest on every run.
    """
    # Stage an orphan W/P under a scenario that doesn't declare workpapers
    orphan_dir = tmp_path / "workpapers" / "q1_pass_dc2_01"  # DC-2 scenario = no W/P
    orphan_dir.mkdir(parents=True)
    orphan = orphan_dir / "stale_from_old_run.xlsx"
    orphan.write_bytes(b"stale")

    generate_gold(_MANIFEST_PATH, tmp_path)

    assert not orphan.exists()
    assert not orphan_dir.exists()  # DC-2 scenarios declare no W/Ps → no dir


# ── error paths ───────────────────────────────────────────────────────


def test_generate_gold_raises_on_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="manifest not found"):
        generate_gold(tmp_path / "nope.yaml", tmp_path)


def test_generate_gold_creates_output_dir_if_absent(tmp_path: Path) -> None:
    nested = tmp_path / "fresh" / "gold"
    generate_gold(_MANIFEST_PATH, nested)
    assert (nested / "tocs").is_dir()
    assert len(list((nested / "tocs").glob("*.xlsx"))) == 20


# ── strip_workbook_metadata helper ───────────────────────────────────


def test_strip_workbook_metadata_pins_timestamps() -> None:
    wb = Workbook()
    wb.properties.creator = "real-user-name"
    wb.properties.lastModifiedBy = "real-user-name"
    wb.properties.created = dt.datetime(2099, 5, 15, 12, 30, 45)
    wb.properties.title = "Confidential"

    strip_workbook_metadata(wb)

    assert wb.properties.creator == ""
    assert wb.properties.lastModifiedBy == ""
    assert wb.properties.created == _FIXED_EPOCH
    assert wb.properties.modified == _FIXED_EPOCH
    assert wb.properties.title == ""


# ── zip-entry determinism verified at artifact level ─────────────────


def test_generated_xlsx_entries_use_fixed_timestamp(tmp_path: Path) -> None:
    """Every zip entry inside the produced .xlsx must carry the fixed epoch.

    Protects against a regression where someone removes ``_normalize_zip``
    and idempotency silently breaks on wall-clock drift.
    """
    generate_gold(_MANIFEST_PATH, tmp_path)
    xlsx_path = next((tmp_path / "tocs").glob("*.xlsx"))
    with zipfile.ZipFile(xlsx_path) as z:
        for info in z.infolist():
            assert info.date_time == _ZIP_FIXED_DATE_TIME, (
                f"{info.filename} has timestamp {info.date_time}, expected {_ZIP_FIXED_DATE_TIME}"
            )


# ── main() CLI entry ─────────────────────────────────────────────────


def test_main_returns_zero_on_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--manifest", str(_MANIFEST_PATH), "--output-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    # Output message says "Wrote N files" — N is now 50 (40 TOC artefacts + 10 W/Ps)
    assert "50" in out
    assert str(tmp_path) in out


def test_main_writes_forty_toc_artefacts(tmp_path: Path) -> None:
    main(["--manifest", str(_MANIFEST_PATH), "--output-dir", str(tmp_path)])
    toc_files = list((tmp_path / "tocs").iterdir())
    assert len(toc_files) == 40


def test_main_writes_ten_billing_calc_workpapers(tmp_path: Path) -> None:
    """Task 12 — every DC-9 scenario declares a billing_calc; DC-2 scenarios do not."""
    main(["--manifest", str(_MANIFEST_PATH), "--output-dir", str(tmp_path)])
    wp_root = tmp_path / "workpapers"
    billing_calcs = list(wp_root.rglob("billing_calc.xlsx"))
    assert len(billing_calcs) == 10
    # Each lives under workpapers/<scenario_id>/billing_calc.xlsx
    for p in billing_calcs:
        assert p.parent.parent == wp_root
        assert "dc9" in p.parent.name, f"billing_calc emitted under non-DC9 scenario: {p}"


# ── --hash-manifest-path flag + write_hash_manifest helper ───────────


def test_write_hash_manifest_produces_one_line_per_xlsx(tmp_path: Path) -> None:
    generate_gold(_MANIFEST_PATH, tmp_path)
    baseline = tmp_path / "corpus_hashes.txt"
    count = write_hash_manifest(tmp_path, baseline)

    # 20 TOCs + 10 billing-calc W/Ps (Task 12 — DC-9 scenarios only)
    assert count == 30
    lines = baseline.read_text().splitlines()
    assert len(lines) == 30


def test_write_hash_manifest_lines_are_relative_path_space_hash(tmp_path: Path) -> None:
    generate_gold(_MANIFEST_PATH, tmp_path)
    baseline = tmp_path / "corpus_hashes.txt"
    write_hash_manifest(tmp_path, baseline)

    for line in baseline.read_text().splitlines():
        rel_path, digest = line.split()
        # Path is corpus-root-relative (POSIX); lives under tocs/ or workpapers/
        assert rel_path.startswith(("tocs/", "workpapers/"))
        assert (tmp_path / rel_path).exists()
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


def test_write_hash_manifest_is_sorted_by_relative_path(tmp_path: Path) -> None:
    generate_gold(_MANIFEST_PATH, tmp_path)
    baseline = tmp_path / "corpus_hashes.txt"
    write_hash_manifest(tmp_path, baseline)

    paths = [line.split()[0] for line in baseline.read_text().splitlines()]
    assert paths == sorted(paths)


def test_write_hash_manifest_walks_subtree_recursively(tmp_path: Path) -> None:
    """Baseline must cover every .xlsx under corpus_root — tocs/ and
    workpapers/ subtrees both appear, no manual enumeration required.
    """
    generate_gold(_MANIFEST_PATH, tmp_path)
    baseline = tmp_path / "corpus_hashes.txt"
    count = write_hash_manifest(tmp_path, baseline)

    # Task 12 produces 20 tocs + 10 billing-calc W/Ps = 30 total
    assert count == 30
    rel_paths = [line.split()[0] for line in baseline.read_text().splitlines()]
    # Spot-check that workpapers entries actually appear
    assert any(p.startswith("workpapers/") and p.endswith("/billing_calc.xlsx") for p in rel_paths)
    assert any(p.startswith("tocs/") and p.endswith("_ref.xlsx") for p in rel_paths)


def test_generate_gold_xlsx_stems_end_with_ref(tmp_path: Path) -> None:
    """Regression guard for Q7.15 naming decision — every reference TOC
    carries the _ref suffix; bare <scenario_id>.xlsx is forbidden.
    """
    generate_gold(_MANIFEST_PATH, tmp_path)
    for p in (tmp_path / "tocs").glob("*.xlsx"):
        assert p.stem.endswith("_ref"), f"{p.name} missing _ref suffix"


def test_write_hash_manifest_creates_parent_dir(tmp_path: Path) -> None:
    generate_gold(_MANIFEST_PATH, tmp_path)
    nested = tmp_path / "deep" / "nested" / "corpus_hashes.txt"
    write_hash_manifest(tmp_path, nested)
    assert nested.is_file()


def test_write_hash_manifest_is_deterministic(tmp_path: Path) -> None:
    """Same corpus → same baseline, byte-for-byte."""
    generate_gold(_MANIFEST_PATH, tmp_path)
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    write_hash_manifest(tmp_path, a)
    write_hash_manifest(tmp_path, b)
    assert a.read_bytes() == b.read_bytes()


def test_main_with_hash_manifest_flag_writes_baseline(tmp_path: Path) -> None:
    baseline = tmp_path / "corpus_hashes.txt"
    rc = main(
        [
            "--manifest",
            str(_MANIFEST_PATH),
            "--output-dir",
            str(tmp_path),
            "--hash-manifest-path",
            str(baseline),
        ]
    )
    assert rc == 0
    assert baseline.is_file()
    # 20 TOCs + 10 billing-calc W/Ps (Task 12)
    assert len(baseline.read_text().splitlines()) == 30


def test_main_without_hash_manifest_flag_does_not_write_baseline(tmp_path: Path) -> None:
    """Flag is opt-in; default invocation produces no baseline file."""
    baseline = tmp_path / "corpus_hashes.txt"
    main(["--manifest", str(_MANIFEST_PATH), "--output-dir", str(tmp_path)])
    assert not baseline.exists()
