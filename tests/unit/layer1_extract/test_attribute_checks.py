"""Stub-level tests for `attribute_checks.check_attribute`.

The 10 inner functions are placeholders that all return
``status="pass"``. These tests verify the dispatch wiring and contract
boundary — task_03 will add the per-(control, attribute) behavior tests
once real check logic lands.

What's tested here:

- All 10 registered (control, attribute) pairs dispatch to a function
  that returns a valid `AttributeCheck` with ``status="pass"`` and the
  expected control/attribute IDs.
- ``check_attribute("DC-2", "E", ...)`` raises `KeyError` because DC-2
  doesn't define attribute E.
- ``check_attribute("DC-2", "F", ...)`` same — fail-fast on attribute
  IDs the control doesn't carry.
- The stub note marker is present so future readers can `grep` for
  unfinished checks.
"""

from __future__ import annotations

import pytest

from agentic_audit.layer1_extract.attribute_checks import (
    _STUB_NOTE,
    check_attribute,
)
from agentic_audit.models.evidence import (
    ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
)

# ---------- happy paths --------------------------------------------------


@pytest.mark.parametrize(
    ("control_id", "attribute_id"),
    [(c, a) for c, attrs in ATTRIBUTES_PER_CONTROL.items() for a in attrs],
)
def test_dispatch_returns_pass_for_every_registered_pair(
    control_id: str,
    attribute_id: str,
) -> None:
    result = check_attribute(control_id, attribute_id, rows=[], toc=None)  # type: ignore[arg-type]
    assert isinstance(result, AttributeCheck)
    assert result.control_id == control_id
    assert result.attribute_id == attribute_id
    assert result.status == "pass"
    assert result.evidence_cell_refs == []
    assert result.notes == _STUB_NOTE


# ---------- dispatch error paths -----------------------------------------


def test_dc2_attribute_e_raises_key_error() -> None:
    """DC-2 only defines A-D; E is a contract violation."""
    with pytest.raises(KeyError, match="DC-2 does not define attribute_id=E"):
        check_attribute("DC-2", "E", rows=[], toc=None)  # type: ignore[arg-type]


def test_dc2_attribute_f_raises_key_error() -> None:
    with pytest.raises(KeyError, match="DC-2 does not define attribute_id=F"):
        check_attribute("DC-2", "F", rows=[], toc=None)  # type: ignore[arg-type]
