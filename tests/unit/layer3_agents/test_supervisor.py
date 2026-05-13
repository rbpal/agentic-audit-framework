"""Tests for ``agentic_audit.layer3_agents.supervisor`` — task_02.

Three contracts pinned here:

1. ``is_layer3_eligible`` correctly composes ``control_id`` + ``attribute_id``
   for the v1 (DC-9.D, DC-2.B) reservation set and rejects everything
   else. Parametrized truth table — keeps the trigger from silently
   widening or narrowing under future edits.
2. The compiled LangGraph has the four expected nodes; the Mermaid
   diagram is queryable. Catches accidental node-name drift between
   the schema task and the routing task.
3. ``run_investigation`` runs end-to-end with the stub nodes for both
   eligible scopes, preserves scope fields, and refuses ineligible /
   mismatched inputs. The stub routes ``START → supervisor → END`` so
   the returned ``status`` is the initial ``"investigating"`` — that
   becomes a real terminal-state assertion when task_03 lands.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentic_audit.layer3_agents.supervisor import (
    compiled_graph,
    is_layer3_eligible,
    run_investigation,
)
from agentic_audit.models.evidence import (
    ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)

UTC_TS = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


def _evidence(
    control_id: str,
    quarter: str,
    engagement_id: str = "eng-1",
) -> ExtractedEvidence:
    return ExtractedEvidence(
        engagement_id=engagement_id,
        control_id=control_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        run_id="run-1",
        extraction_timestamp=UTC_TS,
        preparer=SignOff(initials="JD", role="preparer", date=UTC_TS),
        reviewer=SignOff(initials="MR", role="reviewer", date=UTC_TS),
        attributes=[
            AttributeCheck(
                control_id=control_id,  # type: ignore[arg-type]
                attribute_id=a,  # type: ignore[arg-type]
                status="pass",
            )
            for a in ATTRIBUTES_PER_CONTROL[control_id]
        ],
        source_bronze_file_hash="abc",
    )


# ── is_layer3_eligible ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("control_id", "attribute_id", "status", "expected"),
    [
        # Eligible — fails on the two Layer-3-reserved attributes.
        ("DC-9", "D", "fail", True),
        ("DC-2", "B", "fail", True),
        # Wrong status — pass / n/a on the right attribute is not eligible.
        ("DC-9", "D", "pass", False),
        ("DC-9", "D", "n/a", False),
        ("DC-2", "B", "pass", False),
        # Right control, wrong attribute — DC-9.A is a Layer-2 attribute.
        ("DC-9", "A", "fail", False),
        ("DC-9", "B", "fail", False),
        ("DC-9", "C", "fail", False),
        ("DC-9", "E", "fail", False),
        ("DC-9", "F", "fail", False),
        # Right control, wrong attribute — DC-2 reserves only B.
        ("DC-2", "A", "fail", False),
        ("DC-2", "C", "fail", False),
        ("DC-2", "D", "fail", False),
    ],
)
def test_is_layer3_eligible(
    control_id: str, attribute_id: str, status: str, expected: bool
) -> None:
    check = AttributeCheck(
        control_id=control_id,  # type: ignore[arg-type]
        attribute_id=attribute_id,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
    )
    assert is_layer3_eligible(check) is expected


# ── Compiled graph topology ──────────────────────────────────────────


def test_compiled_graph_has_expected_nodes() -> None:
    """All four logical nodes are wired. Renaming any of these without
    updating the supervisor's routing dispatch (task_03) would silently
    break the graph; pinning the names here catches the rename at
    unit-test time."""
    nodes = set(compiled_graph.get_graph().nodes.keys())
    assert {"supervisor", "extraction_agent", "validation_agent", "narrative_agent"} <= nodes


def test_compiled_graph_draws_mermaid() -> None:
    """draw_mermaid exercises the full topology and renders every node
    label. A regression that broke graph compilation would surface
    here without needing to invoke a node."""
    diagram = compiled_graph.get_graph().draw_mermaid()
    assert "supervisor" in diagram
    assert "extraction_agent" in diagram
    assert "validation_agent" in diagram
    assert "narrative_agent" in diagram


# ── run_investigation ────────────────────────────────────────────────


def test_run_investigation_billing_rate_change_happy_path() -> None:
    """End-to-end with task_03 supervisor + task_02 sub-agent stubs.

    The supervisor routes to extraction (findings still None after
    each stub return) for three iterations, then hits the cap and
    escalates. status flips to escalated_to_human; the trace carries
    three supervisor entries. Scope fields are preserved through the
    invocation."""
    check = AttributeCheck(control_id="DC-9", attribute_id="D", status="fail")
    result = run_investigation(
        check=check,
        current=_evidence("DC-9", "Q3"),
        prior=_evidence("DC-9", "Q2"),
        agent_run_id="sweep-001",
    )
    assert result["exception_type"] == "billing_rate_change"
    assert result["control_id"] == "DC-9"
    assert result["attribute_id"] == "D"
    assert result["quarter"] == "Q3"
    assert result["engagement_id"] == "eng-1"
    assert result["agent_run_id"] == "sweep-001"
    assert result["investigation_run_id"].startswith("inv-")
    assert result["status"] == "escalated_to_human"
    assert result["iterations_used"] == 3
    assert len(result["investigation_log"]) == 3
    assert result["extraction_findings"] is None


def test_run_investigation_variance_plausibility_happy_path() -> None:
    check = AttributeCheck(control_id="DC-2", attribute_id="B", status="fail")
    result = run_investigation(
        check=check,
        current=_evidence("DC-2", "Q3"),
        prior=_evidence("DC-2", "Q2"),
        agent_run_id="sweep-001",
    )
    assert result["exception_type"] == "variance_plausibility"
    assert result["control_id"] == "DC-2"
    assert result["attribute_id"] == "B"
    assert result["status"] == "escalated_to_human"


def test_run_investigation_rejects_ineligible_check() -> None:
    """The Layer-1 trigger gate is enforced at invocation, not just at
    the caller. A caller that forgets to pre-gate gets a loud ValueError
    instead of a silently-wrong investigation."""
    check = AttributeCheck(control_id="DC-9", attribute_id="A", status="fail")
    with pytest.raises(ValueError, match="not Layer-3 eligible"):
        run_investigation(
            check=check,
            current=_evidence("DC-9", "Q3"),
            prior=_evidence("DC-9", "Q2"),
            agent_run_id="sweep-001",
        )


def test_run_investigation_rejects_passing_check() -> None:
    """Even on a reserved attribute (DC-9.D), a passing status is not
    Layer-3 eligible — no anomaly to investigate."""
    check = AttributeCheck(control_id="DC-9", attribute_id="D", status="pass")
    with pytest.raises(ValueError, match="not Layer-3 eligible"):
        run_investigation(
            check=check,
            current=_evidence("DC-9", "Q3"),
            prior=_evidence("DC-9", "Q2"),
            agent_run_id="sweep-001",
        )


def test_run_investigation_rejects_engagement_mismatch() -> None:
    """current and prior evidence must come from the same engagement —
    Layer 3 reasons about one tenant at a time. A mismatch points to
    a caller bug; surface it at the boundary."""
    check = AttributeCheck(control_id="DC-9", attribute_id="D", status="fail")
    with pytest.raises(ValueError, match="engagement_id mismatch"):
        run_investigation(
            check=check,
            current=_evidence("DC-9", "Q3", engagement_id="eng-1"),
            prior=_evidence("DC-9", "Q2", engagement_id="eng-2"),
            agent_run_id="sweep-001",
        )


def test_run_investigation_rejects_control_mismatch() -> None:
    """check.control_id, current.control_id, and prior.control_id all
    have to agree. The supervisor reasons about one control at a time;
    a mismatch routes the wrong prompt variant downstream."""
    check = AttributeCheck(control_id="DC-9", attribute_id="D", status="fail")
    with pytest.raises(ValueError, match="control_id mismatch"):
        run_investigation(
            check=check,
            current=_evidence("DC-9", "Q3"),
            prior=_evidence("DC-2", "Q2"),
            agent_run_id="sweep-001",
        )
