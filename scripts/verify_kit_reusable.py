#!/usr/bin/env python3
"""Verify ``ai_ops_kit`` is reusable across projects.

Two independent checks (both must pass):

1. **Static self-containment** — grep ``ai_ops_kit/`` for any project-specific
   imports (e.g., ``from agentic_audit``). The kit must never depend on
   Project 1; if it does, Projects 2 and 3 can't consume it cleanly.

2. **Fresh-venv install simulation** — copy ``ai_ops_kit/`` to a temp dir,
   create a fresh venv there, ``pip install -e`` the kit, then run smoke
   imports and the kit's standalone test suite (``ai_ops_kit/tests/``).
   Simulates how Projects 2 and 3 will consume the kit (git submodule +
   isolated venv).

Run from repo root::

    poetry run python scripts/verify_kit_reusable.py

Exit codes: ``0`` = pass, non-zero = failure. CI invokes this on every PR
touching ``ai_ops_kit/`` (see ``.github/workflows/ci.yml``).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KIT_DIR = REPO_ROOT / "ai_ops_kit"

# Project-specific imports the kit must NEVER make. Extend this if the
# project grows new top-level packages outside `agentic_audit`.
FORBIDDEN_IMPORT_PREFIXES = (
    "from agentic_audit",
    "import agentic_audit",
)

# Smoke imports run in the fresh venv. Validates the kit's full public
# surface is importable and basic decorator/context-manager usage works.
SMOKE_SCRIPT = """
from ai_ops_kit import (
    __version__,
    configure_logging,
    get_tracer,
    init_tracer,
    trace_context,
    traced_agent,
    traced_llm_call,
    traced_tool,
)

assert __version__ == "0.1.0", f"version mismatch: {__version__}"

@traced_tool()
def example_tool(x):
    return x * 2

assert example_tool(21) == 42

with trace_context("smoke_pipeline", scenario_id="x"):
    pass

print(f"smoke imports OK, version {__version__}")
"""


def _scan_for_forbidden_imports() -> list[tuple[Path, int, str]]:
    """Return a list of (path, lineno, line) for every forbidden import in the kit."""
    violations: list[tuple[Path, int, str]] = []
    for py_file in KIT_DIR.rglob("*.py"):
        # Skip __pycache__ and the kit's own tests/ (tests can legitimately
        # do anything; the rule is only about kit source code).
        if "__pycache__" in py_file.parts or "tests" in py_file.parts:
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            for forbidden in FORBIDDEN_IMPORT_PREFIXES:
                if stripped.startswith(forbidden):
                    violations.append((py_file.relative_to(REPO_ROOT), lineno, line.rstrip()))
    return violations


def check_self_containment() -> bool:
    """Static check — kit source files must not import from project-specific code."""
    print("[1/2] Static self-containment check")
    violations = _scan_for_forbidden_imports()
    if violations:
        print(f"  FAIL: {len(violations)} forbidden import(s) found in kit source:")
        for path, lineno, line in violations:
            print(f"    {path}:{lineno}: {line}")
        print(f"  Allowed: {', '.join(repr(p) for p in FORBIDDEN_IMPORT_PREFIXES)} — none.")
        return False
    scanned = sum(
        1 for p in KIT_DIR.rglob("*.py") if "__pycache__" not in p.parts and "tests" not in p.parts
    )
    print(f"  OK ({scanned} kit source file(s) scanned)")
    return True


def _venv_executable(venv: Path, name: str) -> str:
    """Locate a venv executable (Windows vs POSIX)."""
    if sys.platform == "win32":
        return str(venv / "Scripts" / f"{name}.exe")
    return str(venv / "bin" / name)


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Wrapper around subprocess.run that captures output for clean error reporting."""
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def check_install_in_fresh_venv() -> bool:
    """Copy kit -> temp dir -> fresh venv -> pip install -e -> smoke + pytest."""
    print("[2/2] Fresh-venv install simulation")
    with tempfile.TemporaryDirectory(prefix="aiopskit_verify_") as tmp:
        tmp_path = Path(tmp)
        kit_copy = tmp_path / "ai_ops_kit"
        venv = tmp_path / "venv"

        print(f"  Copying kit -> {kit_copy}")
        shutil.copytree(
            KIT_DIR,
            kit_copy,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )

        print(f"  Creating fresh venv at {venv}")
        try:
            _run([sys.executable, "-m", "venv", str(venv)])
        except subprocess.CalledProcessError as exc:
            print(f"  FAIL: venv creation failed\n  STDERR: {exc.stderr}")
            return False

        pip = _venv_executable(venv, "pip")
        python = _venv_executable(venv, "python")

        # Upgrade pip first — fresh venvs ship with versions that sometimes
        # reject modern PEP 621 metadata.
        _run([pip, "install", "--upgrade", "pip"], check=False)

        print("  Installing kit (editable) + dev extras")
        result = _run([pip, "install", "-e", f"{kit_copy}[dev]"], check=False)
        if result.returncode != 0:
            print("  FAIL: pip install -e failed")
            print(f"  STDOUT:\n{result.stdout}")
            print(f"  STDERR:\n{result.stderr}")
            return False

        print("  Running smoke imports")
        result = _run([python, "-c", SMOKE_SCRIPT], check=False)
        if result.returncode != 0:
            print("  FAIL: smoke imports failed")
            print(f"  STDOUT:\n{result.stdout}")
            print(f"  STDERR:\n{result.stderr}")
            return False
        print(f"  {result.stdout.strip()}")

        print("  Running kit's standalone pytest suite")
        result = _run(
            [python, "-m", "pytest", str(kit_copy / "tests"), "-q", "--no-header"],
            check=False,
        )
        # pytest's output goes to stdout; print regardless of pass/fail so the
        # user sees the test summary.
        print(result.stdout)
        if result.returncode != 0:
            print(f"  FAIL: kit pytest suite failed\n  STDERR:\n{result.stderr}")
            return False
    return True


def main() -> int:
    if not KIT_DIR.exists():
        print(f"ERROR: kit not found at {KIT_DIR}", file=sys.stderr)
        return 2

    print(f"Verifying ai_ops_kit at {KIT_DIR}")
    print()

    ok = check_self_containment()
    print()
    ok = check_install_in_fresh_venv() and ok
    print()

    if ok:
        print("ai_ops_kit is reusable across projects")
        return 0
    print("ai_ops_kit reusability check FAILED — see errors above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
