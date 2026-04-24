"""CLI entrypoint ``generate-gold`` — regenerates the v2 engagement corpus.

Invoked via ``poetry run generate-gold``. Reads ``manifest.yaml`` as a
v2 ``EngagementSpec`` (one engagement × 4 quarters × 2 controls), then
writes 17 files to the corpus root:

* 1 ``tocs/engagement_toc_ref.xlsx`` (TOC with DC-2 + DC-9 sheets)
* 8 ``tocs/<control>_<quarter>.json`` gold answers
* 8 ``workpapers/<control>_<quarter>_ref.xlsx`` per-quarter W/Ps

Idempotency invariant: running the command twice produces byte-identical
outputs. Three sources of non-determinism are neutralized:

1. openpyxl writes creator / timestamp docprops on every ``save()``
   → ``strip_workbook_metadata`` pins them to fixed values.
2. Python's ``zipfile`` stamps each entry with the current wall clock
   → ``_normalize_zip`` rewrites the archive with 1980-01-01 entry
   timestamps and a stable create-system marker.
3. openpyxl also overrides ``dcterms:modified`` in ``docProps/core.xml``
   at save time → ``_normalize_zip`` patches it back to the fixed epoch.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import sys
import zipfile
from pathlib import Path

from openpyxl import Workbook

from agentic_audit.generator import content_hash
from agentic_audit.generator.engagement_writers import (
    render_dc2_quarter,
    render_dc9_quarter,
    render_engagement_toc,
)
from agentic_audit.models.engagement import ControlId, EngagementSpec, Quarter, load_engagement
from agentic_audit.models.engagement_gold_answer import (
    build_quarter_gold_answer,
    engagement_gold_answer_to_json,
)

_FIXED_EPOCH = dt.datetime(2000, 1, 1, 0, 0, 0)
_ZIP_FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)

# openpyxl overrides ``dcterms:modified`` with wall-clock UTC at save time,
# even when we pin ``wb.properties.modified`` before save. Rewrite it in
# the serialized core.xml to restore determinism.
_MODIFIED_RE = re.compile(rb"<dcterms:modified[^>]*>[^<]*</dcterms:modified>")
_FIXED_MODIFIED_XML = (
    b'<dcterms:modified xsi:type="dcterms:W3CDTF">2000-01-01T00:00:00Z</dcterms:modified>'
)


def _default_repo_root() -> Path:
    path = Path(__file__).resolve()
    for parent in [path, *path.parents]:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("Could not locate repo root (no pyproject.toml found)")


def strip_workbook_metadata(wb: Workbook) -> None:
    """Pin openpyxl docprops to fixed values for byte-level determinism."""
    props = wb.properties
    props.creator = ""
    props.lastModifiedBy = ""
    props.created = _FIXED_EPOCH
    props.modified = _FIXED_EPOCH
    props.title = ""
    props.subject = ""
    props.description = ""
    props.keywords = ""
    props.category = ""
    props.contentStatus = ""
    props.version = ""
    props.revision = ""
    props.identifier = ""
    props.language = ""


def _normalize_zip(path: Path) -> None:
    """Rewrite ``path`` so every zip entry is byte-deterministic.

    Two sources of wall-clock drift are neutralized:

    * zip entry ``date_time`` — Python's ``zipfile`` stamps each entry
      with current time; we rewrite with 1980-01-01, the earliest value
      the MS-DOS epoch permits.
    * ``docProps/core.xml`` ``dcterms:modified`` — openpyxl overrides it
      with wall-clock UTC at save time; we patch it back to the fixed
      epoch after the fact.
    """
    with zipfile.ZipFile(path, "r") as src:
        entries = [(info, src.read(info.filename)) for info in src.infolist()]

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dst:
        for info, data in entries:
            if info.filename == "docProps/core.xml":
                data = _MODIFIED_RE.sub(_FIXED_MODIFIED_XML, data)
            new_info = zipfile.ZipInfo(filename=info.filename, date_time=_ZIP_FIXED_DATE_TIME)
            new_info.compress_type = zipfile.ZIP_DEFLATED
            new_info.external_attr = info.external_attr
            new_info.create_system = 3  # Unix, fixed across platforms
            dst.writestr(new_info, data)


def _save_deterministic(wb: Workbook, path: Path) -> None:
    """Save workbook to ``path`` with both docprops and zip bytes normalized."""
    strip_workbook_metadata(wb)
    wb.save(path)
    _normalize_zip(path)


def _clear_output_dir(output_dir: Path) -> None:
    """Delete existing ``*.xlsx`` and ``*.json`` so stale scenarios cannot linger."""
    for pattern in ("*.xlsx", "*.json"):
        for path in output_dir.glob(pattern):
            path.unlink()


def _clear_workpapers_dir(corpus_root: Path) -> None:
    """Remove the entire ``workpapers/`` subtree so orphans from a prior
    run cannot linger. Rebuilt from the manifest on every invocation.
    """
    wp_root = corpus_root / "workpapers"
    if wp_root.is_dir():
        shutil.rmtree(wp_root)


def generate_engagement_corpus(manifest_path: Path, corpus_root: Path) -> list[Path]:
    """v2 orchestrator — regenerate the engagement corpus (17 files).

    Writes, under ``corpus_root``:

    * ``tocs/engagement_toc_ref.xlsx``                     (1 file)
    * ``tocs/<control>_<quarter>.json``                   × 8 gold answers
    * ``workpapers/<control>_<quarter>_ref.xlsx``         × 8 W/Ps

    Clears ``tocs/`` and ``workpapers/`` before writing so stale artefacts
    from a prior run cannot linger. ``manifest.yaml`` is untouched.

    Returns the sorted list of every written path.
    """
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest not found: {manifest_path}")

    tocs_dir = corpus_root / "tocs"
    tocs_dir.mkdir(parents=True, exist_ok=True)
    _clear_output_dir(tocs_dir)
    _clear_workpapers_dir(corpus_root)
    workpapers_dir = corpus_root / "workpapers"
    workpapers_dir.mkdir(parents=True, exist_ok=True)

    spec = load_engagement(manifest_path)
    written: list[Path] = []

    # ── Single engagement TOC (2 sheets: DC-2, DC-9) ───────────────
    toc_wb = render_engagement_toc(spec)
    toc_path = tocs_dir / "engagement_toc_ref.xlsx"
    _save_deterministic(toc_wb, toc_path)
    written.append(toc_path)

    # ── 8 per-quarter W/Ps + 8 gold JSONs ──────────────────────────
    controls: tuple[ControlId, ...] = ("DC-2", "DC-9")
    quarters: tuple[Quarter, ...] = ("Q1", "Q2", "Q3", "Q4")
    for control in controls:
        for quarter in quarters:
            # W/P
            wb = _render_workpaper(spec, control, quarter)
            wp_path = workpapers_dir / f"{_slug(control)}_{quarter}_ref.xlsx"
            _save_deterministic(wb, wp_path)
            written.append(wp_path)

            # Gold JSON
            answer = build_quarter_gold_answer(spec, control, quarter)
            json_path = tocs_dir / f"{_slug(control)}_{quarter}.json"
            json_path.write_text(engagement_gold_answer_to_json(answer) + "\n")
            written.append(json_path)

    return sorted(written)


def _render_workpaper(spec: EngagementSpec, control_id: ControlId, quarter: Quarter) -> Workbook:
    """Dispatch to the right v2 writer based on control_id."""
    if control_id == "DC-2":
        return render_dc2_quarter(spec, quarter)
    if control_id == "DC-9":
        return render_dc9_quarter(spec, quarter)
    raise ValueError(f"No v2 writer for control_id={control_id!r}")


def _slug(control_id: ControlId) -> str:
    """``DC-2`` → ``dc2``, ``DC-9`` → ``dc9`` — file-system-safe slug."""
    return control_id.replace("-", "").lower()


def write_hash_manifest(corpus_root: Path, manifest_path: Path) -> int:
    """Write ``<relative_path> <content_hash>`` lines for every .xlsx under ``corpus_root``.

    Walks ``corpus_root`` recursively so the baseline captures tocs/ today
    and any future workpapers/ subtrees without code changes. Paths are
    POSIX-style relative to ``corpus_root`` for cross-platform consistency.
    """
    xlsx_files = sorted(corpus_root.rglob("*.xlsx"))
    lines = [f"{p.relative_to(corpus_root).as_posix()} {content_hash(p)}\n" for p in xlsx_files]
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("".join(lines))
    return len(xlsx_files)


def main(argv: list[str] | None = None) -> int:
    """CLI entry — ``poetry run generate-gold``."""
    parser = argparse.ArgumentParser(
        prog="generate-gold",
        description=(
            "Regenerate the v2 engagement corpus: 1 TOC + 8 gold JSONs + 8 W/Ps "
            "= 17 files under the corpus root."
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to manifest.yaml (default: <repo>/eval/gold_scenarios/manifest.yaml)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Corpus root — tocs/ and future workpapers/ are written inside it "
            "(default: <repo>/eval/gold_scenarios)"
        ),
    )
    parser.add_argument(
        "--hash-manifest-path",
        type=Path,
        default=None,
        help=(
            "If set, also write <relative_path> <content_hash> lines for every "
            ".xlsx under the corpus root. (canonical: tests/fixtures/corpus_hashes.txt)"
        ),
    )
    args = parser.parse_args(argv)

    repo_root = _default_repo_root()
    manifest_path = args.manifest or repo_root / "eval" / "gold_scenarios" / "manifest.yaml"
    corpus_root = args.output_dir or repo_root / "eval" / "gold_scenarios"

    written = generate_engagement_corpus(manifest_path, corpus_root)
    print(f"Wrote {len(written)} files to {corpus_root}")

    if args.hash_manifest_path is not None:
        count = write_hash_manifest(corpus_root, args.hash_manifest_path)
        print(f"Wrote {count} hash lines to {args.hash_manifest_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
