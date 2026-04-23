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
    generate_gold(_MANIFEST_PATH, tmp_path)
    tocs = tmp_path / "tocs"

    specs = load_manifest(_MANIFEST_PATH)
    expected_ids = {spec.scenario_id for spec in specs}
    xlsx_stems = {p.stem for p in tocs.glob("*.xlsx")}
    json_stems = {p.stem for p in tocs.glob("*.json")}

    assert xlsx_stems == expected_ids
    assert json_stems == expected_ids


def test_generate_gold_returns_sorted_paths(tmp_path: Path) -> None:
    written = generate_gold(_MANIFEST_PATH, tmp_path)
    assert written == sorted(written)
    assert len(written) == 40
    for p in written:
        assert p.parent.name == "tocs", f"{p} is not under tocs/"


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


def test_generate_gold_does_not_touch_siblings_of_tocs(tmp_path: Path) -> None:
    """Future workpapers/ subtree must survive regen of tocs/ — the cleanup
    is scoped to tocs/ only.
    """
    sibling = tmp_path / "workpapers" / "some_scenario"
    sibling.mkdir(parents=True)
    placeholder = sibling / "billing_calc.xlsx"
    placeholder.write_bytes(b"placeholder for future workpaper")

    generate_gold(_MANIFEST_PATH, tmp_path)

    assert placeholder.exists()
    assert placeholder.read_bytes() == b"placeholder for future workpaper"


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
    assert "40" in out
    assert str(tmp_path) in out


def test_main_writes_forty_files(tmp_path: Path) -> None:
    main(["--manifest", str(_MANIFEST_PATH), "--output-dir", str(tmp_path)])
    files = list((tmp_path / "tocs").iterdir())
    assert len(files) == 40


# ── --hash-manifest-path flag + write_hash_manifest helper ───────────


def test_write_hash_manifest_produces_one_line_per_xlsx(tmp_path: Path) -> None:
    generate_gold(_MANIFEST_PATH, tmp_path)
    baseline = tmp_path / "corpus_hashes.txt"
    count = write_hash_manifest(tmp_path, baseline)

    assert count == 20
    lines = baseline.read_text().splitlines()
    assert len(lines) == 20


def test_write_hash_manifest_lines_are_relative_path_space_hash(tmp_path: Path) -> None:
    generate_gold(_MANIFEST_PATH, tmp_path)
    baseline = tmp_path / "corpus_hashes.txt"
    write_hash_manifest(tmp_path, baseline)

    for line in baseline.read_text().splitlines():
        rel_path, digest = line.split()
        # Path is corpus-root-relative (POSIX), resolvable from corpus root
        assert rel_path.startswith("tocs/")
        assert (tmp_path / rel_path).exists()
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


def test_write_hash_manifest_is_sorted_by_relative_path(tmp_path: Path) -> None:
    generate_gold(_MANIFEST_PATH, tmp_path)
    baseline = tmp_path / "corpus_hashes.txt"
    write_hash_manifest(tmp_path, baseline)

    paths = [line.split()[0] for line in baseline.read_text().splitlines()]
    assert paths == sorted(paths)


def test_write_hash_manifest_walks_subtree_recursively(tmp_path: Path) -> None:
    """Baseline must cover every .xlsx under corpus_root, including future
    workpapers/ entries — not just the top level.
    """
    generate_gold(_MANIFEST_PATH, tmp_path)
    # Stage a fake workpaper to prove recursive walking
    wp_dir = tmp_path / "workpapers" / "q1_pass_dc9_01"
    wp_dir.mkdir(parents=True)
    fake_wp = wp_dir / "billing_calc.xlsx"
    # Reuse a real xlsx so it's a valid zip
    fake_wp.write_bytes((tmp_path / "tocs").glob("*.xlsx").__next__().read_bytes())

    baseline = tmp_path / "corpus_hashes.txt"
    count = write_hash_manifest(tmp_path, baseline)

    assert count == 21  # 20 tocs + 1 staged workpaper
    rel_paths = [line.split()[0] for line in baseline.read_text().splitlines()]
    assert "workpapers/q1_pass_dc9_01/billing_calc.xlsx" in rel_paths


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
    assert len(baseline.read_text().splitlines()) == 20


def test_main_without_hash_manifest_flag_does_not_write_baseline(tmp_path: Path) -> None:
    """Flag is opt-in; default invocation produces no baseline file."""
    baseline = tmp_path / "corpus_hashes.txt"
    main(["--manifest", str(_MANIFEST_PATH), "--output-dir", str(tmp_path)])
    assert not baseline.exists()
