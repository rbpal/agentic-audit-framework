"""Tests for ``agentic_audit.generator.content_hash``.

Covers the helper's contract:
* Returns a stable hex SHA-256 string for the same input.
* Same unpacked content → same hash regardless of zip envelope changes
  (entry timestamp, compression method, entry insertion order).
* Different unpacked content → different hash.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from agentic_audit.generator.content_hash import content_hash


def _make_zip(path: Path, entries: list[tuple[str, bytes]]) -> None:
    """Helper: build a zip with the given (name, data) entries in order."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)


# ── shape + determinism ──────────────────────────────────────────────


def test_content_hash_returns_64_char_hex(tmp_path: Path) -> None:
    zpath = tmp_path / "a.xlsx"
    _make_zip(zpath, [("a.xml", b"<x/>")])
    h = content_hash(zpath)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_content_hash_deterministic_for_same_input(tmp_path: Path) -> None:
    zpath = tmp_path / "a.xlsx"
    _make_zip(zpath, [("a.xml", b"<x/>"), ("b.xml", b"<y/>")])
    assert content_hash(zpath) == content_hash(zpath)


# ── envelope independence ────────────────────────────────────────────


def test_content_hash_ignores_entry_order(tmp_path: Path) -> None:
    """Same (name, data) pairs packed in different order → same hash."""
    a = tmp_path / "a.xlsx"
    b = tmp_path / "b.xlsx"
    _make_zip(a, [("one.xml", b"A"), ("two.xml", b"B")])
    _make_zip(b, [("two.xml", b"B"), ("one.xml", b"A")])
    assert content_hash(a) == content_hash(b)


def test_content_hash_ignores_entry_timestamps(tmp_path: Path) -> None:
    """Different zip entry date_time fields → same hash (content unchanged)."""
    a = tmp_path / "a.xlsx"
    b = tmp_path / "b.xlsx"
    with zipfile.ZipFile(a, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo("x.xml", date_time=(1980, 1, 1, 0, 0, 0))
        z.writestr(info, b"payload")
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo("x.xml", date_time=(2099, 12, 31, 23, 59, 58))
        z.writestr(info, b"payload")
    assert content_hash(a) == content_hash(b)


def test_content_hash_ignores_compression_method(tmp_path: Path) -> None:
    """ZIP_STORED vs ZIP_DEFLATED with same content → same hash."""
    a = tmp_path / "a.xlsx"
    b = tmp_path / "b.xlsx"
    with zipfile.ZipFile(a, "w", zipfile.ZIP_STORED) as z:
        z.writestr("x.xml", b"payload")
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("x.xml", b"payload")
    assert content_hash(a) == content_hash(b)


# ── sensitivity ──────────────────────────────────────────────────────


def test_content_hash_differs_when_data_changes(tmp_path: Path) -> None:
    a = tmp_path / "a.xlsx"
    b = tmp_path / "b.xlsx"
    _make_zip(a, [("x.xml", b"one")])
    _make_zip(b, [("x.xml", b"two")])
    assert content_hash(a) != content_hash(b)


def test_content_hash_differs_when_filename_changes(tmp_path: Path) -> None:
    a = tmp_path / "a.xlsx"
    b = tmp_path / "b.xlsx"
    _make_zip(a, [("one.xml", b"same")])
    _make_zip(b, [("two.xml", b"same")])
    assert content_hash(a) != content_hash(b)


def test_content_hash_length_prefix_prevents_boundary_collision(tmp_path: Path) -> None:
    """``"foo" + "bar"`` must not hash the same as ``"fooba" + "r"``."""
    a = tmp_path / "a.xlsx"
    b = tmp_path / "b.xlsx"
    _make_zip(a, [("foo", b"bar")])
    _make_zip(b, [("fooba", b"r")])
    assert content_hash(a) != content_hash(b)


# ── error paths ──────────────────────────────────────────────────────


def test_content_hash_raises_on_nonexistent_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        content_hash(tmp_path / "nope.xlsx")


def test_content_hash_raises_on_non_zip(tmp_path: Path) -> None:
    p = tmp_path / "plain.txt"
    p.write_text("not a zip")
    with pytest.raises(zipfile.BadZipFile):
        content_hash(p)
