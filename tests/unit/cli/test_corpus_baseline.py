"""Tripwire: the committed corpus must still hash to the committed baseline.

Cross-run regression detector that closes the gap left by Task 7's
in-process determinism tests. Task 7 proves "two runs in the same
pytest invocation agree." This file proves "today's corpus on disk
agrees with the baseline committed to main."

Baseline format — lines of ``<relative_path> <sha256>`` where
``<relative_path>`` is corpus-root-relative POSIX (e.g. ``tocs/q1_pass_dc9_01.xlsx``).
This shape carries location, so the tripwire scales to future
``workpapers/<scenario_id>/*.xlsx`` entries without a format change.

If this test fails, one of two things happened:

1. The generator changed semantically (intended) → regenerate the
   baseline and commit it in the same PR::

       poetry run generate-gold \\
           --hash-manifest-path tests/fixtures/corpus_hashes.txt

2. The generator broke (unintended) → fix the bug; do NOT refresh
   the baseline.
"""

from __future__ import annotations

from pathlib import Path

from agentic_audit.generator.content_hash import content_hash

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CORPUS_ROOT = _REPO_ROOT / "eval" / "gold_scenarios"
_BASELINE_PATH = _REPO_ROOT / "tests" / "fixtures" / "corpus_hashes.txt"


def _parse_baseline() -> dict[str, str]:
    """Return ``{relative_path: sha256}`` parsed from the baseline file."""
    mapping: dict[str, str] = {}
    for raw in _BASELINE_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rel_path, digest = line.split()
        mapping[rel_path] = digest
    return mapping


def test_baseline_file_exists() -> None:
    assert _BASELINE_PATH.is_file(), (
        f"Missing baseline {_BASELINE_PATH}. Regenerate with:\n"
        f"  poetry run generate-gold --hash-manifest-path "
        f"{_BASELINE_PATH.relative_to(_REPO_ROOT)}"
    )


def test_baseline_has_twenty_entries() -> None:
    """Matches the 20 scenarios × 1 TOC today. Grows when workpapers land."""
    assert len(_parse_baseline()) == 20


def test_every_baseline_entry_matches_committed_xlsx() -> None:
    """Core tripwire: committed .xlsx content must hash to the baseline value."""
    baseline = _parse_baseline()
    drift: list[str] = []
    for rel_path, expected in baseline.items():
        xlsx_path = _CORPUS_ROOT / rel_path
        assert xlsx_path.exists(), f"{rel_path} missing from {_CORPUS_ROOT}"
        actual = content_hash(xlsx_path)
        if actual != expected:
            drift.append(f"  {rel_path}: baseline={expected[:12]}… current={actual[:12]}…")

    if drift:
        msg = (
            "Corpus has drifted from committed baseline:\n"
            + "\n".join(drift)
            + "\n\nIf this drift is intentional, regenerate the baseline:\n"
            + "  poetry run generate-gold --hash-manifest-path tests/fixtures/corpus_hashes.txt"
        )
        raise AssertionError(msg)


def test_no_committed_xlsx_missing_from_baseline() -> None:
    baseline_paths = set(_parse_baseline().keys())
    committed_paths = {p.relative_to(_CORPUS_ROOT).as_posix() for p in _CORPUS_ROOT.rglob("*.xlsx")}
    extra = committed_paths - baseline_paths
    assert not extra, f"xlsx committed without baseline entry: {sorted(extra)}"


def test_no_baseline_entry_missing_committed_xlsx() -> None:
    baseline_paths = set(_parse_baseline().keys())
    committed_paths = {p.relative_to(_CORPUS_ROOT).as_posix() for p in _CORPUS_ROOT.rglob("*.xlsx")}
    missing = baseline_paths - committed_paths
    assert not missing, f"baseline entry without committed xlsx: {sorted(missing)}"


def test_baseline_is_sorted_by_relative_path() -> None:
    """Sorted form keeps baseline diffs readable in PRs."""
    lines = [line.strip() for line in _BASELINE_PATH.read_text().splitlines() if line.strip()]
    paths = [line.split()[0] for line in lines]
    assert paths == sorted(paths), "baseline must be sorted by relative path"


def test_all_baseline_paths_are_under_tocs() -> None:
    """Step 1.5 Task 10 invariant — only tocs/ entries exist today. When
    workpapers/ lands in Task 12+, extend this assertion or split by prefix.
    """
    for rel_path in _parse_baseline():
        assert rel_path.startswith("tocs/"), (
            f"Unexpected baseline path {rel_path!r} — expected prefix 'tocs/'"
        )
