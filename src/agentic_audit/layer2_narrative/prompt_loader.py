"""Read versioned prompt templates from disk.

Per Decision 6.2 in ``privateDocs/step_05_layer2_narrative.md``: prompt
templates are text files in ``prompts/`` versioned in git. The version
string maps directly to a filename — ``"v1.0"`` resolves to
``"v1_0.txt"``. Generators pin the prompt version at init time so every
narrative row in ``audit_dev.gold.narratives`` carries the exact
prompt version it was generated with.

No fallback, no auto-discovery: explicit version pinning is the
contract. A missing version file is a hard error, not a silent default.
"""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(version: str) -> str:
    """Read the prompt template for ``version`` from disk.

    The filename convention replaces dots with underscores and appends
    ``.txt`` — ``"v1.0"`` → ``"v1_0.txt"``, ``"v1.1"`` → ``"v1_1.txt"``.

    Raises:
        ValueError: ``version`` is empty.
        FileNotFoundError: no prompt file exists for that version.
    """
    if not version:
        raise ValueError("version must be a non-empty string")
    filename = version.replace(".", "_") + ".txt"
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")
