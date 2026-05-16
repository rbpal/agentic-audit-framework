"""Layer 3 ReAct-agent tools.

The three tools the Extraction sub-agent binds via
``create_react_agent``. **`read_billing_rate` has a real body**
(Step 8 task_01); the other two stay as placeholders until task_02
+ task_03 swap them in.

Real-body tools read from the supervisor-loaded ``InvestigationState``
via LangGraph 1.x's ``InjectedState`` binding (Step 8 task_00 locked
this mechanism after evaluating closure-factory + per-call silver
round-trip alternatives — see ``privateDocs/step_08_agent_tools.md``
§ Design rationale). The state parameter is a side-door: the LLM
sees only the LLM-facing args (``engagement_id``, ``control_id``,
``quarter``); the framework slips the live state in at call-time.

Signatures + return shapes are FIXED. A reshape of any return dict
is a breaking change to the agent prompts (which reference field
names in their few-shot examples); the LLM-facing arg list is
similarly load-bearing. Step-8 work changes BODIES, not contracts.

Returned dicts are JSON-serialisable plain dicts (not pydantic
models). LangChain wraps each tool's output as a ToolMessage string;
introducing pydantic models here would force serialisation
round-trips without buying us validation that we can't already get
from the agent's structured-output mode on the FINAL response.
"""

from __future__ import annotations

from typing import Annotated, Any, NotRequired

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from langgraph.prebuilt.chat_agent_executor import AgentState

from agentic_audit.layer3_agents.state import ExtractionFindings, InvestigationState
from agentic_audit.models.evidence import AttributeCheck, ExtractedEvidence


class _ExtractionReActState(InvestigationState, AgentState):  # type: ignore[misc,unused-ignore]
    """State schema for the Extraction ReAct loop.

    Composes the supervisor's ``InvestigationState`` (engagement /
    control / evidence / findings) with LangGraph's ``AgentState``
    (``messages`` + ``remaining_steps``) AND adds ``structured_response``
    which ``create_react_agent`` requires whenever ``response_format``
    is set (we set it to ``ExtractionFindings`` so the ReAct loop
    emits a parsed pydantic instance, not free-form prose).

    Lives in ``tools.py`` (not ``state.py``) because it's the
    annotation target for ``InjectedState`` on the tool parameters
    here; ``extraction_agent.py`` imports it back from this module
    to pass into ``create_react_agent(state_schema=...)``. The
    import direction (extraction_agent → tools) was already
    established by the tool-binding pattern; reversing it would be
    a circular import.

    PoC verification of this composition: Step 8 task_00's
    ``scratch/step_08_task_00_binding_poc.py`` proved the
    ``InjectedState`` end-to-end wiring against the live tenant
    (~$0.005, 4 messages, 1 tool call). The two prerequisites it
    surfaced (``typing_extensions.TypedDict`` in ``state.py`` +
    this composition) are both landed in task_01.
    """

    # response_format=ExtractionFindings on the agent requires this
    # key to be in the state_schema. NotRequired because it's
    # populated by the agent on exit, not by the supervisor on entry.
    structured_response: NotRequired[ExtractionFindings | None]


# ── read_billing_rate ────────────────────────────────────────────────


def _resolve_evidence_for_quarter(
    state: _ExtractionReActState, quarter: str
) -> ExtractedEvidence | None:
    """Pick the ``ExtractedEvidence`` matching the LLM-passed quarter.

    The supervisor loads exactly two evidence payloads per
    investigation: ``current_quarter_evidence`` (the quarter under
    investigation) and ``prior_quarter_evidence`` (the comparison
    quarter). The LLM passes the desired quarter as a string;
    matching against both surfaces the right one. Returns ``None``
    if the LLM asked for a quarter that's neither — surfaces as
    ``rate=None`` in the tool's return, which the agent loop handles
    cleanly (it's the same shape as "attribute not found").
    """
    current = state.get("current_quarter_evidence")
    if current is not None and current.quarter == quarter:
        return current
    prior = state.get("prior_quarter_evidence")
    if prior is not None and prior.quarter == quarter:
        return prior
    return None


def _find_attribute_check(evidence: ExtractedEvidence, attribute_id: str) -> AttributeCheck | None:
    """Pull the ``AttributeCheck`` for a specific attribute id off the
    evidence's per-attribute list. Returns ``None`` if the attribute
    isn't in the control's attribute set (e.g., asking for DC-9.D
    when the evidence is for DC-2)."""
    for check in evidence.attributes:
        if check.attribute_id == attribute_id:
            return check
    return None


def _coerce_rate(value: Any) -> float | None:
    """Best-effort cast of ``AttributeCheck.extracted_value`` to a
    float. The field is typed ``Any | None`` upstream because Layer 1
    extraction stores raw cell values without per-attribute
    normalisation; for DC-9.D specifically Layer 1 actually stores a
    dict shape ``{"current_rate": X, "prior_rate": Y, "rate_change":
    bool}`` (discovered by the Step 8 task_05 live verification run,
    2026-05-16 — task_01 had assumed bare numeric per the placeholder
    contract). This helper now handles four input shapes:

    - ``None`` → None
    - ``int`` / ``float`` → float (the assumed shape; still supported
      so synthetic test fixtures don't need to wrap in a dict)
    - ``str`` parseable as float → float (Layer 1 occasionally
      serialises rates as strings depending on source cell type)
    - ``dict`` with a ``"current_rate"`` key → recurse on that value
      (the real Layer-1 shape; "current_rate" is the rate FOR THE
      QUARTER OF THIS ROW, not vs another quarter — each Q's silver
      row carries its own current_rate)

    Anything else degrades to None rather than raising.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        return _coerce_rate(value.get("current_rate"))
    return None


# ── read_billing_rate ────────────────────────────────────────────────


# DC-9.D is the only attribute this tool projects against — the
# billing rate lives on attribute "D" of control "DC-9" per the master
# plan. Hardcoded here (rather than a tool arg) because the prompt's
# few-shot pattern doesn't pass attribute_id either: this tool is
# specific to DC-9.D, not a general per-attribute reader. If a future
# control wires its rate to a different attribute, lift this constant
# into a per-control map at that time.
_DC9D_ATTRIBUTE_ID = "D"


@tool
def read_billing_rate(
    engagement_id: str,
    control_id: str,
    quarter: str,
    state: Annotated[_ExtractionReActState, InjectedState],
) -> dict[str, Any]:
    """Look up the billing rate the auditor recorded for a
    (engagement, control, quarter) triple.

    Used by the Extraction sub-agent on the DC-9.D billing-rate-change
    path to anchor the rate-delta computation. Call once with the
    current quarter and once with the prior quarter, then diff.

    Args:
        engagement_id: Engagement identifier (e.g., ``"eng-2025-alpha"``).
            Currently informational — the supervisor only loads one
            engagement's evidence into state, so this arg is
            cross-checked against ``state.engagement_id`` for
            sanity, not used for routing.
        control_id: SOX control identifier — ``"DC-9"`` for the
            billing-rate path.
        quarter: Audit period (``"Q1"``..``"Q4"``). Resolves against
            ``state.current_quarter_evidence`` first, then
            ``state.prior_quarter_evidence``.

    Returns:
        Dict with keys:
            - ``rate`` (float | None): Recorded billing rate; None if
              the attribute or quarter isn't in state, or if
              extracted_value can't coerce to numeric.
            - ``rate_unit`` (str): Hardcoded ``"basis_points"`` for
              DC-9.D per the master plan.
            - ``source_cell_ref`` (str): First entry from the
              attribute check's ``evidence_cell_refs`` (workpaper
              lineage anchor). Empty string if no refs recorded.
            - ``recorded_at`` (str): ISO-8601 timestamp from
              ``ExtractedEvidence.extraction_timestamp``.
            - ``notes`` (str): Auditor note attached to the attribute
              check; empty string if none.

    Note: ``state`` is injected by LangGraph at call-time via
    ``InjectedState`` (Step 8 task_00). The LLM does NOT see this
    parameter — it's a side-door for the tool to access the
    supervisor's pre-loaded evidence without a per-call warehouse
    round-trip.
    """
    evidence = _resolve_evidence_for_quarter(state, quarter)
    if evidence is None:
        return {
            "rate": None,
            "rate_unit": "basis_points",
            "source_cell_ref": "",
            "recorded_at": "",
            "notes": f"quarter {quarter!r} not in supervisor-loaded evidence",
        }

    check = _find_attribute_check(evidence, _DC9D_ATTRIBUTE_ID)
    if check is None:
        return {
            "rate": None,
            "rate_unit": "basis_points",
            "source_cell_ref": "",
            "recorded_at": evidence.extraction_timestamp.isoformat(),
            "notes": (
                f"attribute {_DC9D_ATTRIBUTE_ID!r} not present in {control_id} evidence "
                f"for {quarter}"
            ),
        }

    rate = _coerce_rate(check.extracted_value)
    source_cell_ref = check.evidence_cell_refs[0] if check.evidence_cell_refs else ""
    return {
        "rate": rate,
        "rate_unit": "basis_points",
        "source_cell_ref": source_cell_ref,
        "recorded_at": evidence.extraction_timestamp.isoformat(),
        "notes": check.notes or "",
    }


# ── compare_billing_rates ────────────────────────────────────────────


# Substring markers we treat as "this AttributeCheck.notes references an
# amendment." Case-insensitive match — Layer 1 doesn't normalise note
# casing, so "IMA amendment" / "ima amendment" / "Amendment dated..."
# all need to register. Keep the list short; broader heuristics risk
# false positives that would surface non-amendment notes as amendment
# text.
_AMENDMENT_MARKERS = ("amendment", "ima ", "addendum", "side letter")


def _detect_amendment_in_notes(notes: str | None) -> str | None:
    """Return the notes verbatim if they look like an amendment marker,
    else None.

    The Layer-1 extractor doesn't have a dedicated IMA-amendment column
    on silver yet (Step 8 doc § task_02 enumerates this as the open
    data-availability question). Best-effort substring scan of the
    DC-9.D ``AttributeCheck.notes`` field is the seam available without
    a schema migration: when a reviewer attaches an amendment reference
    to their note, surface it; otherwise degrade to "not found."

    Real amendment-text extraction lives at the silver-schema layer
    (extend silver with ``ima_amendment_text`` + ``ima_amendment_cell_ref``
    columns AND extend Layer 1 to populate them from the bronze IA Note
    sheet). Tracked as a Step 9 follow-up — Step 8 stays bounded to
    "real bodies for the existing tool shape."
    """
    if not notes:
        return None
    lower = notes.lower()
    for marker in _AMENDMENT_MARKERS:
        if marker in lower:
            return notes
    return None


@tool
def compare_billing_rates(
    engagement_id: str,
    control_id: str,
    current_quarter: str,
    prior_quarter: str,
    state: Annotated[_ExtractionReActState, InjectedState],
) -> dict[str, Any]:
    """Compute the rate delta between two quarters and surface any
    governing-document amendment that authorises a change.

    Used by the Extraction sub-agent to skip the two-call read +
    manual-diff pattern when it just needs the delta + amendment
    pointer. The Validation sub-agent then judges whether the
    amendment is sufficient (task_05).

    Args:
        engagement_id: Engagement identifier (currently informational
            — the supervisor only loads one engagement's evidence into
            state).
        control_id: SOX control identifier — ``"DC-9"`` for the
            billing-rate path.
        current_quarter: Quarter under investigation. Resolves against
            ``state.current_quarter_evidence`` / prior, same pattern
            as ``read_billing_rate``.
        prior_quarter: Comparison quarter (typically the immediately
            prior one in the engagement's calendar).

    Returns:
        Dict with keys:
            - ``current_rate`` (float | None): None-safe; absent when
              the current_quarter isn't in state or the DC-9.D check
              is missing.
            - ``prior_rate`` (float | None): same null shape.
            - ``delta`` (float | None): ``current - prior`` when both
              present; ``None`` otherwise.
            - ``percent_change`` (float | None): ``(current - prior) /
              prior`` when prior > 0; ``None`` when prior is missing,
              zero, or negative.
            - ``ima_amendment_found`` (bool): True iff the current
              quarter's DC-9.D ``AttributeCheck.notes`` carries an
              amendment-shaped marker.
            - ``ima_amendment_text`` (str): The notes verbatim when
              the marker hit; empty string otherwise.
            - ``ima_amendment_cell_ref`` (str): First entry from the
              current quarter's DC-9.D ``evidence_cell_refs`` (the
              lineage anchor for the amendment evidence) when the
              amendment hit; empty string otherwise.

    Note: ``state`` is injected by LangGraph at call-time via
    ``InjectedState``. The LLM does NOT see this parameter.

    Amendment-surface design (Step 8 task_02):

    Two enumerated options + the implemented third in the Step 8 doc:

    - Option (a) — extend silver with dedicated amendment columns:
      cleaner long-term but scope-creep risk for Step 8 (schema
      migration + Layer-1 extractor update + re-baseline).
    - Option (b) — wire a bronze IA-Note read inside this tool:
      adds warehouse-round-trip latency + bronze-reader plumbing in
      ``tools.py``.
    - Option (c) — **implemented here:** best-effort substring scan
      of the existing ``AttributeCheck.notes`` for amendment markers.
      Pure-state read (no warehouse round-trip), no schema change,
      consistent with ``read_billing_rate``'s pattern. Degrades to
      "not found" when notes are unstructured or absent. Real amendment
      extraction lives at silver-schema / Layer-1 extractor layer
      and is tracked as a Step 9 follow-up.
    """
    current_evidence = _resolve_evidence_for_quarter(state, current_quarter)
    prior_evidence = _resolve_evidence_for_quarter(state, prior_quarter)

    current_check = (
        _find_attribute_check(current_evidence, _DC9D_ATTRIBUTE_ID)
        if current_evidence is not None
        else None
    )
    prior_check = (
        _find_attribute_check(prior_evidence, _DC9D_ATTRIBUTE_ID)
        if prior_evidence is not None
        else None
    )

    current_rate = _coerce_rate(current_check.extracted_value) if current_check else None
    prior_rate = _coerce_rate(prior_check.extracted_value) if prior_check else None

    delta: float | None
    percent_change: float | None
    if current_rate is not None and prior_rate is not None:
        delta = current_rate - prior_rate
        # Guard against prior=0 (division by zero) AND prior<0 (negative
        # rates aren't a thing for billing rates; the percent_change
        # would be misleading sign-wise even if non-zero).
        percent_change = (current_rate - prior_rate) / prior_rate if prior_rate > 0 else None
    else:
        delta = None
        percent_change = None

    # Amendment surface — best-effort scan of current quarter's notes.
    # The amendment that justifies a Q3 rate change lives ON the Q3
    # evidence (the auditor attaches the reference to the period under
    # investigation), so prior's notes are not consulted.
    amendment_text: str | None = None
    amendment_cell_ref = ""
    if current_check is not None:
        amendment_text = _detect_amendment_in_notes(current_check.notes)
        if amendment_text is not None and current_check.evidence_cell_refs:
            amendment_cell_ref = current_check.evidence_cell_refs[0]

    return {
        "current_rate": current_rate,
        "prior_rate": prior_rate,
        "delta": delta,
        "percent_change": percent_change,
        "ima_amendment_found": amendment_text is not None,
        "ima_amendment_text": amendment_text or "",
        "ima_amendment_cell_ref": amendment_cell_ref,
    }


# ── read_reviewer_comments ───────────────────────────────────────────


@tool
def read_reviewer_comments(
    engagement_id: str,
    control_id: str,
    quarter: str,
    attribute_id: str,
    state: Annotated[_ExtractionReActState, InjectedState],
) -> dict[str, Any]:
    """Pull the reviewer's free-text comments for a specific attribute.

    Used by the Extraction sub-agent on the DC-2.B variance-
    plausibility path to surface the auditor's explanation note for a
    flagged variance. Without this the validation sub-agent can't
    judge plausibility against the rationale.

    Args:
        engagement_id: Engagement identifier (informational; the
            supervisor only loads one engagement's evidence).
        control_id: SOX control (``"DC-2"`` in v1's variance path,
            but the tool projects per-attribute generically).
        quarter: Audit period. Resolves against
            ``state.current_quarter_evidence`` first, then
            ``state.prior_quarter_evidence``.
        attribute_id: Single-letter attribute (``"B"`` for variance
            plausibility).

    Returns:
        Dict with keys:
            - ``comments`` (list[str]): Reviewer comments in
              chronological order. Empty list when none recorded.
              At v1's silver shape there's at most one note per
              attribute check; this list normalises future multi-
              note shapes without breaking the prompt contract.
            - ``variance_explanation_found`` (bool): True iff the
              attribute check has any non-empty notes content.
              Variance explanations are less narrowly defined than
              IMA amendments (any reviewer note on DC-2.B IS the
              explanation), so no substring-marker scan is needed.
            - ``variance_explanation_text`` (str): The explanation
              body when found; empty string otherwise.
            - ``source_cell_refs`` (list[str]): Lineage anchors —
              the attribute check's ``evidence_cell_refs`` verbatim.

    Note: ``state`` is injected by LangGraph at call-time via
    ``InjectedState``. The LLM does NOT see this parameter.

    Data-source decision (Step 8 task_03): picks **Option (a)** —
    pure-state read from ``AttributeCheck.notes`` (silver-side notes
    the reviewer attached during Layer-1 extraction). Consistent with
    task_02's Option (c) pure-state pattern (the spec's
    "don't half-bronze the system" warning). Limit: notes may not
    carry the full variance-explanation prose if the reviewer wrote
    it elsewhere — tracked as Step 9 follow-up alongside the IMA-
    amendment silver-schema extension.
    """
    evidence = _resolve_evidence_for_quarter(state, quarter)
    if evidence is None:
        return {
            "comments": [],
            "variance_explanation_found": False,
            "variance_explanation_text": "",
            "source_cell_refs": [],
        }

    check = _find_attribute_check(evidence, attribute_id)
    if check is None:
        return {
            "comments": [],
            "variance_explanation_found": False,
            "variance_explanation_text": "",
            "source_cell_refs": [],
        }

    # Notes IS the variance explanation on the v1 silver shape. No
    # marker-substring scan here (unlike task_02's amendment surface)
    # because any non-empty note on DC-2.B is the explanation by
    # construction — there's no other category of note for this
    # attribute that we'd need to distinguish from.
    notes_present = bool(check.notes)
    return {
        "comments": [check.notes] if notes_present else [],
        "variance_explanation_found": notes_present,
        "variance_explanation_text": check.notes or "",
        "source_cell_refs": list(check.evidence_cell_refs),
    }


__all__ = [
    "compare_billing_rates",
    "read_billing_rate",
    "read_reviewer_comments",
    "_ExtractionReActState",
]
