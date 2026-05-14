"""LangGraph ``StateGraph`` for the Layer 3 supervisor.

This module wires the four-node graph, exposes ``run_investigation``,
and ships the rule-based supervisor + conditional-edge router. All
three sub-agent wrappers are now real:

- task_04 — ``extraction_agent_node`` (wraps ``ExtractionAgent``)
- task_05 — ``validation_agent_node`` (wraps ``ValidationAgent``)
- task_06 — ``narrative_agent_node`` (wraps ``NarrativeAgent``)

Topology (matches ``privateDocs/step_07_layer3_multiagent.md`` task_02)::

    START → supervisor
    supervisor → {extraction_agent, validation_agent, narrative_agent,
                  conclude (=END), escalate (=END)}
    extraction_agent → supervisor
    validation_agent → supervisor
    narrative_agent  → supervisor

Supervisor decision rules (task_03, rule-based, NOT LLM):

1. ``iterations_used >= MAX_ITERATIONS`` → ``"escalate"``
2. ``extraction_findings is None`` → ``"extraction"``
3. ``validation_findings is None`` → ``"validation"``
4. ``final_narrative is None`` → ``"narrative"``
5. ``final_narrative is not None``:
   - ``confidence_score >= CONFIDENCE_THRESHOLD`` AND ``judge_verdict == "pass"`` → ``"conclude"``
   - otherwise → ``"escalate"``

The judge-gate check is the Step 6 believe-either-fail wiring: the
supervisor only concludes when both the agent's self-reported
confidence AND a structured judge verdict agree. Without that, Layer 3
would conclude on raw confidence alone — the gate exists to catch the
case where the agent is confidently wrong.

The judge call itself happens inside ``supervisor_node`` (the routing
function ``route_from_supervisor`` stays pure). Inject via
``config["configurable"]["layer3_judge"]`` — a ``Layer3JudgeFunc``
``Callable[[InvestigationState], JudgeResponse]``. Defaults to ``None``,
which makes the gate fail-closed: any narrative-bearing state with no
judge configured routes to escalate. Task_07 wires the real
LLM-backed Layer-3 judge through this hook.

Checkpointer is ``InMemorySaver`` (1.x rename of ``MemorySaver``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agentic_audit.layer3_agents.extraction_agent import ExtractionAgent
from agentic_audit.layer3_agents.narrative_agent import NarrativeAgent
from agentic_audit.layer3_agents.state import (
    ExceptionNarrative,
    ExceptionType,
    InvestigationState,
    InvestigationStep,
)
from agentic_audit.layer3_agents.validation_agent import ValidationAgent
from agentic_audit.models.evidence import (
    LAYER3_ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
    ExtractedEvidence,
)
from agentic_audit.models.judge import JudgeResponse

# ── Routing constants ────────────────────────────────────────────────

# 3-iteration ceiling matches the master plan + task_07. Each
# supervisor visit increments the counter BEFORE the cap check, so
# the third visit is the one that escalates. Bounds cost + prevents
# runaway loops; LangGraph's ``recursion_limit=10`` is a belt-and-
# suspenders backstop.
MAX_ITERATIONS: int = 3

# Confidence floor for concluding. Matches Step 6's gate posture —
# the supervisor concludes only when the agent's confidence AND the
# judge agree. Override per-call via ``run_investigation``'s
# ``confidence_threshold`` kwarg when a calibration sweep wants to
# explore the curve.
CONFIDENCE_THRESHOLD: float = 0.7

# Injection key for the Layer-3 judge inside ``RunnableConfig.configurable``.
# Centralised so the supervisor and ``run_investigation`` agree on the
# spelling — typos surface as fail-closed escalates, not silent
# misrouting.
LAYER3_JUDGE_CONFIG_KEY: str = "layer3_judge"

# Injection key for the Extraction sub-agent. Same pattern as the
# judge key — centralised so a typo can't silently disable the agent.
# When absent, ``extraction_agent_node`` returns ``{}`` (stub
# behaviour) and the supervisor iterates until the cap fires.
LAYER3_EXTRACTION_AGENT_CONFIG_KEY: str = "layer3_extraction_agent"

# Injection key for the Validation sub-agent. Same pattern as the
# extraction key — fail-closed when absent (the supervisor keeps
# routing to validation until the cap fires).
LAYER3_VALIDATION_AGENT_CONFIG_KEY: str = "layer3_validation_agent"

# Injection key for the Narrative sub-agent. Fail-closed when absent
# (the supervisor keeps routing to narrative until the cap fires).
LAYER3_NARRATIVE_AGENT_CONFIG_KEY: str = "layer3_narrative_agent"

# ── Layer-3 judge protocol ───────────────────────────────────────────

# Minimal contract task_03 needs from a judge: take the live
# ``InvestigationState`` and return a structured verdict. The supervisor
# reads ``.verdict`` and ``.confidence`` off the result; reasoning +
# cited_evidence_fields are persisted via the trace by task_07 / task_08.
# Reusing the Layer-2 ``JudgeResponse`` shape avoids a parallel model
# until/unless task_07 finds a real divergence.
Layer3JudgeFunc = Callable[[InvestigationState], JudgeResponse]

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


# ── Routing + supervisor (task_03) ───────────────────────────────────


_SUB_AGENT_ROUTES: frozenset[str] = frozenset({"extraction", "validation", "narrative"})


def _decide_route(state: InvestigationState) -> str:
    """Pure routing decision over a single state snapshot.

    Called by both ``supervisor_node`` (to compute the destination
    label for the trace + the terminal-status update) and
    ``route_from_supervisor`` (to actually direct the conditional
    edge). Sharing this function guarantees the log entry, the
    persisted ``status``, and the LangGraph edge cannot disagree.

    Routing semantics:

    1. **Determine intent first** — what would the supervisor do
       absent any cap. The intent is one of: a sub-agent dispatch
       (because some output is still missing), or a terminal decision
       (because all three sub-agent outputs are present).
    2. **Cap-check sub-agent intents only.** If the intent is a
       sub-agent dispatch and ``iterations_used >= MAX_ITERATIONS``,
       override to ``"escalate"``. Terminal intents (``"conclude"`` /
       ``"escalate"``) are NOT cap-gated — a successful 3-iteration
       happy path concludes correctly without the cap blocking it.
    3. **Judge gate fails closed** — terminal "conclude" intent
       requires both ``confidence_score >= CONFIDENCE_THRESHOLD`` AND
       ``judge_verdict == "pass"``. A narrative-bearing state with a
       missing or non-pass judge verdict routes to ``"escalate"``.
       Confident-but-unjudged narratives must not reach gold.

    This ordering (intent → cap) was a task_03 latent bug surfaced by
    task_06's happy-path e2e test: with the cap-first ordering, the
    iteration cap fired on supervisor visit 4 (the visit that
    *would* have concluded) because each sub-agent dispatch counted
    one supervisor visit and the conclude visit pushed iter past the
    ceiling. Reordering preserves the cap's purpose (bound runaway
    sub-agent loops) without trapping the happy path.
    """
    if state.get("extraction_findings") is None:
        intent = "extraction"
    elif state.get("validation_findings") is None:
        intent = "validation"
    elif state.get("final_narrative") is None:
        intent = "narrative"
    else:
        # Terminal decision branch — all three sub-agent outputs in
        # hand. The cap does not gate this branch.
        if (
            state.get("confidence_score", 0.0) >= CONFIDENCE_THRESHOLD
            and state.get("judge_verdict") == "pass"
        ):
            return "conclude"
        return "escalate"

    # Sub-agent dispatch intent — apply the cap.
    if state.get("iterations_used", 0) >= MAX_ITERATIONS:
        return "escalate"
    return intent


# Map of route → terminal Layer3Status. Only conclude/escalate
# transition the state machine; sub-agent dispatch routes leave
# ``status="investigating"`` untouched.
_TERMINAL_STATUS_BY_ROUTE: dict[str, str] = {
    "conclude": "concluded",
    "escalate": "escalated_to_human",
}


def supervisor_node(
    state: InvestigationState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Supervisor — rule-based dispatcher.

    Side-effects per visit:

    1. If a ``final_narrative`` just landed and the judge gate has not
       run yet, calls the injected ``Layer3JudgeFunc`` from ``config``
       and writes ``judge_verdict`` + ``judge_confidence`` into state.
    2. Computes the route via ``_decide_route`` against the
       post-judge-gate state view.
    3. Appends one ``InvestigationStep`` to ``investigation_log`` naming
       the actual destination — the operator.add reducer concatenates
       across visits, so the full chain survives.
    4. On a terminal route (``"conclude"`` / ``"escalate"``) sets
       ``status`` accordingly. Sub-agent dispatch leaves status as
       ``"investigating"``.

    The supervisor does **NOT** increment ``iterations_used`` — that
    counter is owned by the sub-agent nodes (incremented on every
    invocation, including the no-agent-injected no-op path). This
    keeps the route the supervisor decided in lockstep with the route
    ``route_from_supervisor`` re-derives via ``_decide_route``: both
    see the same ``iterations_used`` and produce the same answer.

    Errors raised by the judge propagate — task_07's real Layer-3 judge
    handles its own retries + uncertain-fallback (same posture as the
    Layer-2 ``Judge.evaluate``). The supervisor stays out of that
    business.
    """
    updates: dict[str, Any] = {}

    if state.get("final_narrative") is not None and state.get("judge_verdict") is None:
        judge = (config.get("configurable") or {}).get(LAYER3_JUDGE_CONFIG_KEY)
        if judge is not None:
            judge_fn = cast(Layer3JudgeFunc, judge)
            judgment = judge_fn(state)
            updates["judge_verdict"] = judgment.verdict
            updates["judge_confidence"] = judgment.confidence

    # Decide against the post-judge-gate view so the conclude-vs-
    # escalate check sees the latest verdict.
    pre_route_state = cast(InvestigationState, {**state, **updates})
    route = _decide_route(pre_route_state)

    log_iteration = state.get("iterations_used", 0)
    updates["investigation_log"] = [
        InvestigationStep(
            iteration=log_iteration,
            actor="supervisor",
            action=f"route_to_{route}",
            timestamp=datetime.now(UTC),
        )
    ]

    if route in _TERMINAL_STATUS_BY_ROUTE:
        updates["status"] = _TERMINAL_STATUS_BY_ROUTE[route]

    return updates


def route_from_supervisor(state: InvestigationState) -> str:
    """Conditional-edge function — pure read of the post-supervisor
    state. The actual decision lives in ``_decide_route``; this wrapper
    exists because LangGraph's ``add_conditional_edges`` API takes a
    callable, not a dict."""
    return _decide_route(state)


# ── Sub-agent nodes ──────────────────────────────────────────────────


def _no_agent_updates(state: InvestigationState, actor: str) -> dict[str, Any]:
    """Build the state delta a sub-agent node returns when no agent is
    injected.

    Two side-effects:

    1. ``iterations_used`` increments — sub-agent nodes own the
       counter. Without this, LangGraph's super-step engine sees no
       state delta when extraction returns from a no-op node and
       terminates the graph early (before the supervisor can apply
       the cap and route to escalate).
    2. A ``no_agent_injected_no_op`` trace entry is appended so the
       investigation log records the empty visit — operators reading
       the trace can see the cap was hit because no agent was wired,
       not because the agent declined to populate findings.
    """
    new_iter = state.get("iterations_used", 0) + 1
    return {
        "iterations_used": new_iter,
        "investigation_log": [
            InvestigationStep(
                iteration=new_iter,
                actor=actor,
                action="no_agent_injected_no_op",
                timestamp=datetime.now(UTC),
            )
        ],
    }


def _agent_ran_updates(
    state: InvestigationState,
    actor: str,
    action: str,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the state delta a sub-agent node returns when its agent
    actually ran. Increments ``iterations_used`` (same counter
    semantics as the no-op branch — the iteration counts the visit,
    not its outcome) and appends a trace entry naming the actor +
    action. ``extra`` carries the agent's output payload (e.g.
    ``{"extraction_findings": findings}``)."""
    new_iter = state.get("iterations_used", 0) + 1
    updates: dict[str, Any] = {
        "iterations_used": new_iter,
        "investigation_log": [
            InvestigationStep(
                iteration=new_iter,
                actor=actor,
                action=action,
                timestamp=datetime.now(UTC),
            )
        ],
    }
    if extra:
        updates.update(extra)
    return updates


def extraction_agent_node(
    state: InvestigationState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Run the Extraction sub-agent if one is injected; otherwise no-op.

    Reads an ``ExtractionAgent`` instance from
    ``config["configurable"][LAYER3_EXTRACTION_AGENT_CONFIG_KEY]``.
    When present, invokes it against the live state and writes
    ``extraction_findings`` + a trace entry. When absent, emits a
    ``no_agent_injected_no_op`` trace entry — same fail-closed posture
    as the judge gate (the supervisor will loop until the iteration
    cap fires), but the trace entry keeps the LangGraph super-step
    engine from declaring early convergence on a no-state-delta
    return.

    The Step 8 tools the agent binds are placeholders today
    (``tools.py`` — canned empty payloads). Real tool bodies land in
    Step 8 without touching this wiring; the agent + supervisor stay
    untouched on that swap.
    """
    agent_obj = (config.get("configurable") or {}).get(LAYER3_EXTRACTION_AGENT_CONFIG_KEY)
    if agent_obj is None:
        return _no_agent_updates(state, "extraction_agent")
    agent = cast(ExtractionAgent, agent_obj)
    findings = agent.invoke(state)
    return _agent_ran_updates(
        state,
        "extraction_agent",
        "emitted_extraction_findings",
        extra={"extraction_findings": findings},
    )


def validation_agent_node(
    state: InvestigationState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Run the Validation sub-agent if one is injected; otherwise no-op.

    Reads a ``ValidationAgent`` instance from
    ``config["configurable"][LAYER3_VALIDATION_AGENT_CONFIG_KEY]``.
    When present, invokes it against the live state (which carries the
    Extraction agent's findings the validation prompt is built from)
    and writes ``validation_findings`` + a trace entry. When absent,
    returns ``{}`` — same fail-closed posture as the extraction node:
    the supervisor will loop until the iteration cap fires.

    The validation invoke may short-circuit to the no-document fast
    path (returning ``ValidationFindings(is_authorized=False,
    confidence=0.9, reasoning="No supporting document found")``)
    without an LLM call. Either path emits the same trace shape — the
    fast vs LLM split is an internal detail of ``ValidationAgent``.
    """
    agent_obj = (config.get("configurable") or {}).get(LAYER3_VALIDATION_AGENT_CONFIG_KEY)
    if agent_obj is None:
        return _no_agent_updates(state, "validation_agent")
    agent = cast(ValidationAgent, agent_obj)
    findings = agent.invoke(state)
    return _agent_ran_updates(
        state,
        "validation_agent",
        "emitted_validation_findings",
        extra={"validation_findings": findings},
    )


def narrative_agent_node(
    state: InvestigationState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Run the Narrative sub-agent if one is injected; otherwise no-op.

    Reads a ``NarrativeAgent`` instance from
    ``config["configurable"][LAYER3_NARRATIVE_AGENT_CONFIG_KEY]``.
    When present, invokes it against the live state (which carries
    both extraction + validation findings the narrative prompt grounds
    against), writes ``final_narrative`` + a trace entry + updates
    ``confidence_score`` from the validation finding's confidence so
    the supervisor's conclude-vs-escalate gate can compare against
    ``CONFIDENCE_THRESHOLD``.

    ``confidence_score`` choice: ``validation_findings.confidence`` is
    the load-bearing signal for "should this exception be acted on
    automatically". Extraction confidence reflects how well we located
    the facts; validation confidence reflects how trustworthy the
    authorise/reject verdict is, which is what the supervisor's
    conclude gate is gating on. A high-confidence negative validation
    (e.g. fast-path no-document at 0.9) correctly concludes with an
    ``ExceptionNarrative.recommendation="ESCALATE"`` rather than
    routing to the system-failure "escalate" terminal.

    When the validation finding is missing (defensive — shouldn't
    happen if the supervisor's routing is correct), the field is left
    at its prior value.
    """
    agent_obj = (config.get("configurable") or {}).get(LAYER3_NARRATIVE_AGENT_CONFIG_KEY)
    if agent_obj is None:
        return _no_agent_updates(state, "narrative_agent")
    agent = cast(NarrativeAgent, agent_obj)
    narrative = agent.invoke(state)
    extra: dict[str, Any] = {"final_narrative": narrative}
    validation = state.get("validation_findings")
    if validation is not None:
        extra["confidence_score"] = validation.confidence
    return _agent_ran_updates(
        state,
        "narrative_agent",
        "emitted_final_narrative",
        extra=extra,
    )


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
    judge: Layer3JudgeFunc | None = None,
    extraction_agent: ExtractionAgent | None = None,
    validation_agent: ValidationAgent | None = None,
    narrative_agent: NarrativeAgent | None = None,
) -> InvestigationState:
    """Run one Layer-3 investigation end-to-end.

    Builds the initial ``InvestigationState`` from the failing
    ``AttributeCheck`` plus current + prior quarter evidence, invokes
    the compiled graph, and returns the terminal state.

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
    judge
        Optional Layer-3 judge. Called once when ``final_narrative``
        first lands and its verdict + confidence feed the supervisor's
        conclude-vs-escalate gate. ``None`` (the default) is fail-closed
        — any narrative-bearing state routes to escalate. Task_07 wires
        the real LLM-backed judge through this hook.
    extraction_agent
        Optional Extraction sub-agent. When present, the supervisor's
        ``extraction_agent_node`` invokes it once to populate
        ``extraction_findings``. ``None`` (the default) leaves the node
        as a no-op — the supervisor keeps routing to extraction until
        the iteration cap fires. Production wires
        ``ExtractionAgent.from_env()``.
    validation_agent
        Optional Validation sub-agent. When present, the supervisor's
        ``validation_agent_node`` invokes it once to populate
        ``validation_findings`` (may short-circuit to the no-document
        fast path without an LLM call). ``None`` (the default) leaves
        the node as a no-op — the supervisor keeps routing to
        validation until the iteration cap fires. Production wires
        ``ValidationAgent.from_env()``.
    narrative_agent
        Optional Narrative sub-agent. When present, the supervisor's
        ``narrative_agent_node`` invokes it once to populate
        ``final_narrative`` (≤200-word ``ExceptionNarrative`` with
        ``recommendation ∈ {ACCEPT, ESCALATE}``) and propagate the
        validation finding's confidence into ``confidence_score`` so
        the supervisor's conclude-vs-escalate gate can compare against
        ``CONFIDENCE_THRESHOLD``. ``None`` (the default) leaves the
        node as a no-op — the supervisor keeps routing to narrative
        until the iteration cap fires. Production wires
        ``NarrativeAgent.from_env()``.

    Raises
    ------
    ValueError
        - ``check`` is not Layer-3 eligible.
        - ``current``/``prior`` disagree on engagement_id or control_id.

    Notes
    -----
    With sub-agents still stubbed (task_04–06 pending), an invocation
    that triggers extraction routes ``supervisor → extraction (stub)``
    three times, then escalates at the iteration cap. The terminal
    ``status`` is ``"escalated_to_human"`` and the ``investigation_log``
    carries three supervisor entries. Real terminal-state shapes land
    as those sub-agents fill in.
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
        "judge_verdict": None,
        "judge_confidence": None,
        "confidence_score": 0.0,
        "iterations_used": 0,
        "status": "investigating",
    }

    configurable: dict[str, Any] = {"thread_id": investigation_run_id}
    if judge is not None:
        configurable[LAYER3_JUDGE_CONFIG_KEY] = judge
    if extraction_agent is not None:
        configurable[LAYER3_EXTRACTION_AGENT_CONFIG_KEY] = extraction_agent
    if validation_agent is not None:
        configurable[LAYER3_VALIDATION_AGENT_CONFIG_KEY] = validation_agent
    if narrative_agent is not None:
        configurable[LAYER3_NARRATIVE_AGENT_CONFIG_KEY] = narrative_agent
    config: dict[str, Any] = {
        "configurable": configurable,
        "recursion_limit": 10,
    }
    # LangGraph 1.x ``invoke`` overloads don't match a plain TypedDict
    # input + raw dict config — the stubs want ``RunnableConfig``, a
    # narrower TypedDict. At runtime LangGraph accepts both unchanged.
    # The ``unused-ignore`` partner code is needed because mypy resolves
    # the langgraph stubs differently in whole-package mode vs the
    # single-file pre-commit run; one mode sees the overload error,
    # the other doesn't.
    result = compiled_graph.invoke(initial_state, config=config)  # type: ignore[call-overload,unused-ignore]
    final_state = cast(InvestigationState, result)
    return _ensure_terminal_narrative(final_state)


# ── Degraded escalation narrative (Step 7 task_07) ──────────────────


# Canned text for the degraded-escalation narrative. Verbatim from
# privateDocs/step_07_layer3_multiagent.md task_07. Stable so
# downstream consumers (gold.layer3_decisions readers, the human-
# review UI) can detect this branch by exact-match without parsing
# variable LLM-emitted text.
_DEGRADED_ESCALATION_NARRATIVE_TEXT: str = (
    "Automated investigation could not reach sufficient confidence; human review required."
)


def _build_degraded_escalation_narrative() -> ExceptionNarrative:
    """Build the sentinel ``ExceptionNarrative`` for terminal escalates
    that never reached the narrative sub-agent.

    Two cases produce a final state with ``status="escalated_to_human"``
    AND ``final_narrative=None``:

    1. The iteration cap fired before extraction or validation
       populated their findings (e.g. no agent injected, agent failure).
    2. Validation populated but supervisor escalated before narrative
       (extracted findings + validation present, but cap still fired
       because narrative agent was missing or kept failing).

    The narrative_agent's own ``_fallback_escalate`` covers a third
    case: narrative agent invoked but LLM/fact-check failed twice.
    That fallback embeds a diagnostic reason in the text. This
    helper produces the *clean* canned text — same shape, no
    diagnostic noise — for the two cases above where no LLM was ever
    asked to write a narrative.

    Both shapes ship to ``gold.layer3_decisions`` so every terminal
    state writes a complete row. The ``recommendation="ESCALATE"`` +
    empty ``citations`` together signal the human-review path; the
    ``investigation_log`` (also persisted) carries the supervisor's
    routing trace for the reviewer to read.
    """
    return ExceptionNarrative(
        narrative_text=_DEGRADED_ESCALATION_NARRATIVE_TEXT,
        citations=[],
        recommendation="ESCALATE",
        word_count=len(_DEGRADED_ESCALATION_NARRATIVE_TEXT.split()),
    )


def _ensure_terminal_narrative(state: InvestigationState) -> InvestigationState:
    """Post-invoke wrap — guarantees a terminal escalate state always
    carries a ``final_narrative``.

    No-op if ``status != "escalated_to_human"`` (concluded paths
    always have a real narrative from the narrative agent).
    No-op if ``final_narrative`` is already populated (the narrative
    agent ran and either succeeded or invoked its own fallback).

    Otherwise: set ``final_narrative`` to the canned degraded
    escalation narrative. Mutating-in-place is safe because the
    final state is a fresh dict returned from LangGraph's invoke;
    the caller never sees it before this wrap runs.
    """
    if state.get("status") != "escalated_to_human":
        return state
    if state.get("final_narrative") is not None:
        return state
    state["final_narrative"] = _build_degraded_escalation_narrative()
    return state


__all__ = [
    "CONFIDENCE_THRESHOLD",
    "LAYER3_EXTRACTION_AGENT_CONFIG_KEY",
    "LAYER3_JUDGE_CONFIG_KEY",
    "LAYER3_NARRATIVE_AGENT_CONFIG_KEY",
    "LAYER3_VALIDATION_AGENT_CONFIG_KEY",
    "MAX_ITERATIONS",
    "Layer3JudgeFunc",
    "compiled_graph",
    "extraction_agent_node",
    "is_layer3_eligible",
    "narrative_agent_node",
    "route_from_supervisor",
    "run_investigation",
    "supervisor_node",
    "validation_agent_node",
]
