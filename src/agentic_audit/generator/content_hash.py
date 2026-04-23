"""Content-level SHA-256 hash for a generated workbook.

Hashes the *unpacked* contents of a ``.xlsx`` zip — sorted entry names
and entry bytes — so the result is independent of zip envelope metadata
(entry timestamps, compression flags, entry insertion order). Use this
to answer the question "did the semantic content of this workbook
change?" without noise from the zip wrapper.

Usage:
    from agentic_audit.generator.content_hash import content_hash
    h = content_hash(Path("eval/gold_scenarios/q1_pass_dc9_01.xlsx"))

Invariants this helper enforces in Task 7 tests:
* Two consecutive ``generate-gold`` runs produce identical content hashes
  per workbook.
* Repacking the same zip with different entry timestamps does not change
  the hash.
* Every scenario has a distinct hash (cheap sanity check against
  accidental duplication).
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path


def content_hash(xlsx_path: Path) -> str:
    """Return hex SHA-256 over the sorted (entry-name, entry-bytes) content.

    Length-prefixes name and data so that boundary ambiguity between
    adjacent entries cannot collide — e.g. ``"foo" + "bar"`` can never
    hash the same as ``"fooba" + "r"``.
    """
    h = hashlib.sha256()
    with zipfile.ZipFile(xlsx_path, "r") as z:
        for name in sorted(z.namelist()):
            data = z.read(name)
            h.update(len(name).to_bytes(4, "big"))
            h.update(name.encode("utf-8"))
            h.update(len(data).to_bytes(8, "big"))
            h.update(data)
    return h.hexdigest()
