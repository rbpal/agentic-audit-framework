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
    normalisation; for DC-9.D specifically the value is the billing
    rate as a number (basis points). Strings that parse as numerics
    (``"30.0"``) are tolerated — Layer 1's extractor occasionally
    serialises rates as strings depending on the source cell type.
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


@tool
def compare_billing_rates(
    engagement_id: str,
    control_id: str,
    current_quarter: str,
    prior_quarter: str,
) -> dict[str, Any]:
    """Compute the rate delta between two quarters and surface any
    governing-document amendment that authorises a change.

    Used by the Extraction sub-agent to skip the two-call read +
    manual-diff pattern when it just needs the delta + amendment
    pointer. The Validation sub-agent then judges whether the
    amendment is sufficient (task_05).

    Args:
        engagement_id: Engagement identifier.
        control_id: SOX control (DC-9 in v1).
        current_quarter: Quarter under investigation.
        prior_quarter: Comparison quarter (typically the immediately
            prior one in the engagement's calendar).

    Returns:
        Dict with keys:
            - ``current_rate`` (float | None)
            - ``prior_rate`` (float | None)
            - ``delta`` (float | None): ``current - prior`` when both present.
            - ``percent_change`` (float | None)
            - ``ima_amendment_found`` (bool): Did an IMA amendment land
              between prior and current?
            - ``ima_amendment_text`` (str): Amendment body when found.
            - ``ima_amendment_cell_ref`` (str): Lineage anchor.

    Placeholder: returns an empty payload (no delta, no amendment).
    Step 8 swaps in the joined silver-evidence + cross-file-validations
    read.
    """
    # FIXME(step_08): Replace with real cross-file join.
    return {
        "current_rate": None,
        "prior_rate": None,
        "delta": None,
        "percent_change": None,
        "ima_amendment_found": False,
        "ima_amendment_text": "",
        "ima_amendment_cell_ref": f"<placeholder:{engagement_id}/{control_id}/"
        f"{prior_quarter}->{current_quarter}>",
    }


# ── read_reviewer_comments ───────────────────────────────────────────


@tool
def read_reviewer_comments(
    engagement_id: str,
    control_id: str,
    quarter: str,
    attribute_id: str,
) -> dict[str, Any]:
    """Pull the reviewer's free-text comments for a specific attribute.

    Used by the Extraction sub-agent on the DC-2.B variance-
    plausibility path to surface the auditor's explanation note for a
    flagged variance. Without this the validation sub-agent can't
    judge plausibility against the rationale.

    Args:
        engagement_id: Engagement identifier.
        control_id: SOX control (DC-2 in v1's variance path).
        quarter: Audit period.
        attribute_id: Single-letter attribute (``"B"`` for variance
            plausibility).

    Returns:
        Dict with keys:
            - ``comments`` (list[str]): Reviewer comments in
              chronological order. Empty list when none recorded.
            - ``variance_explanation_found`` (bool): Did the reviewer
              attach a variance explanation note?
            - ``variance_explanation_text`` (str): The explanation
              body when found.
            - ``source_cell_refs`` (list[str]): Lineage anchors per
              comment.

    Placeholder: returns no comments + no explanation. Step 8 swaps
    in the real reader.
    """
    # FIXME(step_08): Replace with real reader over
    # silver.evidence.AttributeCheck.notes + the bronze workpaper.
    return {
        "comments": [],
        "variance_explanation_found": False,
        "variance_explanation_text": "",
        "source_cell_refs": [
            f"<placeholder:{engagement_id}/{control_id}/{quarter}/{attribute_id}>"
        ],
    }


__all__ = [
    "compare_billing_rates",
    "read_billing_rate",
    "read_reviewer_comments",
    "_ExtractionReActState",
]
