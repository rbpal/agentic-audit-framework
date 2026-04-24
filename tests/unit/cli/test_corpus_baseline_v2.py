"""v2 baseline tripwire — committed corpus must hash to committed baseline.

Replaces the v1 tripwire deleted during the cutover. Catches the
cross-run regression v1 tests couldn't see:

* Task 7-style determinism (two regens in one process agree) proves the
  generator is self-consistent **within** a run.
* This test proves **today's committed corpus == the baseline committed
  to main** — i.e., no silent drift between PR and `main`.

If this test fails, one of two things happened:

1. The generator changed semantically (intended) → regenerate the
   baseline in the same PR::

       poetry run generate-gold --hash-manifest-path tests/fixtures/corpus_hashes.txt

2. The generator broke (unintended) → fix the bug; do NOT refresh the
   baseline.

Baseline format is ``<relative_path> <sha256>`` per line — inherited
from Step 1 Task 10.
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


# ── File + shape ─────────────────────────────────────────────────────


def test_baseline_file_exists() -> None:
    assert _BASELINE_PATH.is_file(), (
        f"Missing baseline {_BASELINE_PATH}. Regenerate with:\n"
        f"  poetry run generate-gold --hash-manifest-path "
        f"{_BASELINE_PATH.relative_to(_REPO_ROOT)}"
    )


def test_baseline_has_nine_xlsx_entries() -> None:
    """v2 corpus: 1 TOC + 8 per-quarter W/Ps = 9 xlsx files (JSONs not hashed)."""
    assert len(_parse_baseline()) == 9


# ── Core tripwire: committed content must match committed baseline ──


def test_every_baseline_entry_matches_committed_xlsx() -> None:
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
            + "\n\nIf intentional, regenerate the baseline:\n"
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


# ── v2-specific structural invariants ───────────────────────────────


def test_baseline_has_one_engagement_toc() -> None:
    toc_entries = [p for p in _parse_baseline() if p.startswith("tocs/")]
    assert toc_entries == ["tocs/engagement_toc_ref.xlsx"]


def test_baseline_has_eight_per_quarter_workpapers() -> None:
    wp_entries = sorted(p for p in _parse_baseline() if p.startswith("workpapers/"))
    assert wp_entries == [
        "workpapers/dc2_Q1_ref.xlsx",
        "workpapers/dc2_Q2_ref.xlsx",
        "workpapers/dc2_Q3_ref.xlsx",
        "workpapers/dc2_Q4_ref.xlsx",
        "workpapers/dc9_Q1_ref.xlsx",
        "workpapers/dc9_Q2_ref.xlsx",
        "workpapers/dc9_Q3_ref.xlsx",
        "workpapers/dc9_Q4_ref.xlsx",
    ]


def test_baseline_is_sorted_by_relative_path() -> None:
    """Sorted form keeps baseline diffs readable in PRs."""
    lines = [line.strip() for line in _BASELINE_PATH.read_text().splitlines() if line.strip()]
    paths = [line.split()[0] for line in lines]
    assert paths == sorted(paths)


# ── Cross-artefact integration (end-to-end consistency) ─────────────


def test_every_committed_gold_json_loads_as_engagement_answer() -> None:
    """Each of the 8 gold JSONs must parse back into an EngagementGoldAnswer."""
    from agentic_audit.models.engagement_gold_answer import load_engagement_gold_answer

    json_paths = sorted((_CORPUS_ROOT / "tocs").glob("*.json"))
    assert len(json_paths) == 8

    for p in json_paths:
        answer = load_engagement_gold_answer(p)
        assert answer.control_id in ("DC-2", "DC-9")
        assert answer.quarter in ("Q1", "Q2", "Q3", "Q4")
        # Filename encodes the (control, quarter) — must match the content
        control_slug, quarter = p.stem.split("_")
        expected_control = "DC-2" if control_slug == "dc2" else "DC-9"
        assert answer.control_id == expected_control
        assert answer.quarter == quarter


def test_q3_dc9_gold_has_cross_file_contradiction() -> None:
    """§5 plan: Q3 DC-9 is figure_mismatch; gold JSON must carry the
    cross-file contradiction pointer so the agent can be scored on it.
    """
    from agentic_audit.models.engagement_gold_answer import load_engagement_gold_answer

    answer = load_engagement_gold_answer(_CORPUS_ROOT / "tocs" / "dc9_Q3.json")
    assert answer.defect == "dc9_figure_mismatch"
    assert answer.expected_attribute_results["DC-9.C"] == "fail"
    cfc = answer.expected_cross_file_contradiction
    assert cfc is not None
    assert "dc9_Q3_ref.xlsx" in cfc["wp_cell"]
    assert "Q3" in cfc["toc_cell"]


def test_q1_dc2_gold_is_clean() -> None:
    """Q1 is the baseline clean quarter for both controls."""
    from agentic_audit.models.engagement_gold_answer import load_engagement_gold_answer

    answer = load_engagement_gold_answer(_CORPUS_ROOT / "tocs" / "dc2_Q1.json")
    assert answer.defect == "none"
    assert answer.expected_quarter_verdict == "Effective"
    assert answer.expected_cross_file_contradiction is None
    for letter in "ABCD":
        assert answer.expected_attribute_results[f"DC-2.{letter}"] == "pass"
