"""LangGraph ``StateGraph`` skeleton for the Layer 3 supervisor (Step 7
task_02).

This module wires the four-node graph and exposes the ``run_investigation``
entry point. The nodes themselves are stubs at task_02 — they return
empty partial-state updates so the graph compiles and the topology is
queryable via ``draw_mermaid()``. Real logic lands in:

- task_03 — ``supervisor_node`` + ``route_from_supervisor`` routing rules
- task_04 — ``extraction_agent_node``
- task_05 — ``validation_agent_node``
- task_06 — ``narrative_agent_node``

The stub ``route_from_supervisor`` always returns ``"escalate"`` so the
graph is bounded: ``START → supervisor → END`` in two supersteps. No
recursion-limit risk, no node ever called more than once.

Topology (matches ``privateDocs/step_07_layer3_multiagent.md`` task_02)::

    START → supervisor
    supervisor → {extraction_agent, validation_agent, narrative_agent,
                  conclude (=END), escalate (=END)}
    extraction_agent → supervisor
    validation_agent → supervisor
    narrative_agent  → supervisor

Checkpointer is ``InMemorySaver`` (1.x rename of ``MemorySaver``).
Cross-process resume isn't a v1 requirement; switch to
``PostgresCheckpointer`` if/when supervisor runs need to survive process
restarts (deferred follow-up).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agentic_audit.layer3_agents.state import (
    ExceptionType,
    InvestigationState,
)
from agentic_audit.models.evidence import (
    LAYER3_ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
    ExtractedEvidence,
)

# ── Layer-1 trigger ──────────────────────────────────────────────────


def is_layer3_eligible(check: AttributeCheck) -> bool:
    """Predicate gating which Layer-1 ``AttributeCheck`` instances enter
    the Layer-3 investigation loop.

    An attribute is Layer-3 eligible iff:

    1. ``status == "fail"`` — no point investigating a passed attribute.
    2. ``(control_id, attribute_id)`` is in the Layer-3 reservation
       set. v1 covers DC-9.D (billing rate change) and DC-2.B (variance
       plausibility); the canonical set lives in
       ``models.evidence.LAYER3_ATTRIBUTES_PER_CONTROL`` so the Step 5
       corpus and Step 7's trigger stay aligned by construction.
       Expanding to a third exception type is a one-constant edit.

    The privateDocs task_02 sketch wrote
    ``check.attribute_id in {"DC-9.D", "DC-2.B"}`` — that won't compile
    because ``AttributeCheck.attribute_id`` is the single-letter
    ``Literal["A".."F"]``. The composite eligibility check has to
    consult both fields; this is the correct version.
    """
    if check.status != "fail":
        return False
    eligible_attrs = LAYER3_ATTRIBUTES_PER_CONTROL.get(check.control_id, [])
    return check.attribute_id in eligible_attrs


# (control_id, attribute_id) → exception_type. Mirrors
# LAYER3_ATTRIBUTES_PER_CONTROL but flat — handier for O(1) lookup at
# investigation init time. Add a new entry alongside the canonical set
# when DC-2.A or a third exception type joins (master plan follow-up).
_EXCEPTION_TYPE_BY_SCOPE: dict[tuple[str, str], ExceptionType] = {
    ("DC-9", "D"): "billing_rate_change",
    ("DC-2", "B"): "variance_plausibility",
}


def _infer_exception_type(check: AttributeCheck) -> ExceptionType:
    """Map an eligible ``AttributeCheck`` to its ``ExceptionType``.

    Pre-condition: ``is_layer3_eligible(check)`` is True. Raises
    ``ValueError`` otherwise — callers that forget to gate get a loud
    failure at investigation init, not a silent wrong-prompt routing
    downstream.
    """
    key = (check.control_id, check.attribute_id)
    if key not in _EXCEPTION_TYPE_BY_SCOPE:
        raise ValueError(
            f"No exception_type for ({check.control_id}, {check.attribute_id}). "
            f"Call is_layer3_eligible() before run_investigation()."
        )
    return _EXCEPTION_TYPE_BY_SCOPE[key]


# ── Node stubs (real implementations land in task_03–06) ─────────────


def supervisor_node(state: InvestigationState) -> dict[str, Any]:
    """Supervisor stub — task_03 ships the real routing + iteration
    increment + investigation_log append. For task_02 the node is a
    no-op so the graph compiles and runs end-to-end."""
    return {}


def route_from_supervisor(state: InvestigationState) -> str:
    """Conditional-edge stub — always escalates so task_02's compiled
    graph terminates in two supersteps (START → supervisor → END).
    Task_03 replaces this with the real five-way routing:
    iteration-cap → extraction → validation → narrative → judge-gate
    → conclude/escalate."""
    return "escalate"


def extraction_agent_node(state: InvestigationState) -> dict[str, Any]:
    """Extraction sub-agent stub — task_04 ships ``create_react_agent``
    bound to the Step 8 tools."""
    return {}


def validation_agent_node(state: InvestigationState) -> dict[str, Any]:
    """Validation sub-agent stub — task_05 ships the single-call LLM
    judgment over IMA-amendment / variance-explanation sufficiency."""
    return {}


def narrative_agent_node(state: InvestigationState) -> dict[str, Any]:
    """Narrative sub-agent stub — task_06 ships the
    ``NarrativeGenerator`` + ``FactChecker`` reuse path with the
    exception-narrative prompt variants."""
    return {}


# ── Compiled graph ───────────────────────────────────────────────────


def _build_graph() -> CompiledStateGraph:
    """Wire the four nodes + conditional routing + checkpointer."""
    graph = StateGraph(InvestigationState)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("extraction_agent", extraction_agent_node)
    graph.add_node("validation_agent", validation_agent_node)
    graph.add_node("narrative_agent", narrative_agent_node)

    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        route_from_supervisor,
        {
            "extraction": "extraction_agent",
            "validation": "validation_agent",
            "narrative": "narrative_agent",
            "conclude": END,
            "escalate": END,
        },
    )
    graph.add_edge("extraction_agent", "supervisor")
    graph.add_edge("validation_agent", "supervisor")
    graph.add_edge("narrative_agent", "supervisor")

    return graph.compile(checkpointer=InMemorySaver())


# Module-level singleton. The graph object is stateless across
# invocations — the checkpointer keys per-investigation state by
# ``thread_id`` — so reusing one compile is correct and avoids the
# ~50ms compile cost per call.
compiled_graph: CompiledStateGraph = _build_graph()


# ── Entry point ──────────────────────────────────────────────────────


def _new_investigation_run_id() -> str:
    """Per-invocation identifier. Task_08 will swap in real ULID
    generation aligned with Layer 2's ``narrative_call_id``; for the
    skeleton an ISO-timestamp stand-in is unique-enough and pulls in
    no new dependency."""
    return f"inv-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"


def run_investigation(
    check: AttributeCheck,
    current: ExtractedEvidence,
    prior: ExtractedEvidence,
    *,
    agent_run_id: str,
) -> InvestigationState:
    """Run one Layer-3 investigation end-to-end.

    Builds the initial ``InvestigationState`` from the failing
    ``AttributeCheck`` plus current + prior quarter evidence, invokes
    the compiled graph, and returns the terminal state.

    The privateDocs sketch listed ``(check, prior)`` — but the state
    schema needs both ``current_quarter_evidence`` and
    ``prior_quarter_evidence`` and the supervisor has to know which
    quarter to anchor on, so the actual signature takes ``current``
    explicitly. ``engagement_id`` is read off ``current`` (cross-checked
    against ``prior``) — one less argument to keep in sync.

    Parameters
    ----------
    check
        The failing Layer-1 ``AttributeCheck`` that triggered Layer 3.
        Must satisfy ``is_layer3_eligible``; ``ValueError`` otherwise.
    current
        Full ``ExtractedEvidence`` for the (engagement, control, quarter)
        the check fired on. The supervisor's extraction sub-agent reads
        amendment / explanation evidence off this object (task_04).
    prior
        Same engagement + control, prior quarter. Anchors the rate-delta
        / variance-magnitude computation (task_04 + task_05).
    agent_run_id
        Per-sweep identifier shared across every investigation in the
        same run. Joins to ``gold.cost_telemetry`` for sweep-level
        cost roll-ups (task_08).

    Raises
    ------
    ValueError
        - ``check`` is not Layer-3 eligible.
        - ``current``/``prior`` disagree on engagement_id or control_id.

    Notes
    -----
    Task_02 nodes are stubs: every invocation traverses
    ``START → supervisor → END`` and the returned state equals the
    initial state with ``status="investigating"`` unchanged. Tests
    for terminal-state correctness arrive with task_03+.
    """
    if not is_layer3_eligible(check):
        raise ValueError(
            f"AttributeCheck not Layer-3 eligible: control={check.control_id}, "
            f"attribute={check.attribute_id}, status={check.status}. "
            f"Gate with is_layer3_eligible() before calling run_investigation()."
        )
    if current.engagement_id != prior.engagement_id:
        raise ValueError(
            f"current/prior engagement_id mismatch: {current.engagement_id!r} vs "
            f"{prior.engagement_id!r}. Layer 3 investigates one engagement at a time."
        )
    if current.control_id != prior.control_id:
        raise ValueError(
            f"current/prior control_id mismatch: {current.control_id!r} vs "
            f"{prior.control_id!r}. The supervisor reasons over one control at a time."
        )
    if current.control_id != check.control_id:
        raise ValueError(
            f"check/current control_id mismatch: check={check.control_id!r}, "
            f"current={current.control_id!r}."
        )

    exception_type = _infer_exception_type(check)
    investigation_run_id = _new_investigation_run_id()

    initial_state: InvestigationState = {
        "investigation_run_id": investigation_run_id,
        "agent_run_id": agent_run_id,
        "engagement_id": current.engagement_id,
        "control_id": check.control_id,
        "attribute_id": check.attribute_id,
        "quarter": current.quarter,
        "exception_type": exception_type,
        "current_quarter_evidence": current,
        "prior_quarter_evidence": prior,
        "investigation_log": [],
        "extraction_findings": None,
        "validation_findings": None,
        "final_narrative": None,
        "confidence_score": 0.0,
        "iterations_used": 0,
        "status": "investigating",
    }

    config: dict[str, Any] = {
        "configurable": {"thread_id": investigation_run_id},
        "recursion_limit": 10,
    }
    # LangGraph 1.x ``invoke`` overloads don't match a plain TypedDict
    # input + raw dict config — the stubs want ``RunnableConfig``, a
    # narrower TypedDict. At runtime LangGraph accepts both unchanged.
    # Suppress the overload check on this single line; cast the result
    # back to InvestigationState for caller type-safety.
    result = compiled_graph.invoke(initial_state, config=config)  # type: ignore[call-overload]
    return cast(InvestigationState, result)


__all__ = [
    "compiled_graph",
    "extraction_agent_node",
    "is_layer3_eligible",
    "narrative_agent_node",
    "route_from_supervisor",
    "run_investigation",
    "supervisor_node",
    "validation_agent_node",
]
