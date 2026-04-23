"""Tripwire: the committed corpus must still hash to the committed baseline.

This is the cross-run regression detector that closes the gap left by
Task 7's in-process determinism tests. Task 7 proves "two runs in the
same pytest invocation agree." This file proves "today's corpus on disk
agrees with the baseline committed to main."

If this test fails, one of two things happened:

1. The generator changed semantically (intended) → regenerate the
   baseline and commit it in the same PR:

       poetry run generate-gold \\
           --hash-manifest-path tests/fixtures/corpus_hashes.txt

2. The generator broke (unintended) → fix the bug; do NOT refresh
   the baseline.
"""

from __future__ import annotations

from pathlib import Path

from agentic_audit.generator.content_hash import content_hash

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CORPUS_DIR = _REPO_ROOT / "eval" / "gold_scenarios"
_BASELINE_PATH = _REPO_ROOT / "tests" / "fixtures" / "corpus_hashes.txt"


def _parse_baseline() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in _BASELINE_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        scenario_id, digest = line.split()
        mapping[scenario_id] = digest
    return mapping


def test_baseline_file_exists() -> None:
    assert _BASELINE_PATH.is_file(), (
        f"Missing baseline {_BASELINE_PATH}. Regenerate with:\n"
        f"  poetry run generate-gold --hash-manifest-path {_BASELINE_PATH.relative_to(_REPO_ROOT)}"
    )


def test_baseline_has_twenty_entries() -> None:
    assert len(_parse_baseline()) == 20


def test_every_baseline_entry_matches_committed_xlsx() -> None:
    """Core tripwire: committed .xlsx content must hash to the baseline value."""
    baseline = _parse_baseline()
    drift: list[str] = []
    for scenario_id, expected in baseline.items():
        xlsx_path = _CORPUS_DIR / f"{scenario_id}.xlsx"
        assert xlsx_path.exists(), f"{scenario_id}.xlsx missing from {_CORPUS_DIR}"
        actual = content_hash(xlsx_path)
        if actual != expected:
            drift.append(f"  {scenario_id}: baseline={expected[:12]}… current={actual[:12]}…")

    if drift:
        msg = (
            "Corpus has drifted from committed baseline:\n"
            + "\n".join(drift)
            + "\n\nIf this drift is intentional, regenerate the baseline:\n"
            + "  poetry run generate-gold --hash-manifest-path tests/fixtures/corpus_hashes.txt"
        )
        raise AssertionError(msg)


def test_no_committed_xlsx_missing_from_baseline() -> None:
    baseline_ids = set(_parse_baseline().keys())
    committed_ids = {p.stem for p in _CORPUS_DIR.glob("*.xlsx")}
    extra = committed_ids - baseline_ids
    assert not extra, f"xlsx committed without baseline entry: {sorted(extra)}"


def test_no_baseline_entry_missing_committed_xlsx() -> None:
    baseline_ids = set(_parse_baseline().keys())
    committed_ids = {p.stem for p in _CORPUS_DIR.glob("*.xlsx")}
    missing = baseline_ids - committed_ids
    assert not missing, f"baseline entry without committed xlsx: {sorted(missing)}"


def test_baseline_is_sorted_by_scenario_id() -> None:
    """Sorted form keeps baseline diffs readable in PRs."""
    lines = [line.strip() for line in _BASELINE_PATH.read_text().splitlines() if line.strip()]
    ids = [line.split()[0] for line in lines]
    assert ids == sorted(ids), "baseline must be sorted by scenario_id"
