"""Layer 3 ReAct-agent tools — task_04 placeholders.

The three tools the Extraction sub-agent binds via
``create_react_agent``. **Bodies are placeholders.** Each returns a
canned shape that lets the agent loop progress + the prompt examples
make sense; real silver/bronze reads land in Step 8.

Signatures + return shapes are FIXED here. Step 8 swaps the bodies
without touching the agent prompts or the supervisor wiring — that's
the whole point of landing them now. A reshape of any return dict
is a breaking change to the agent prompts (which reference field
names in their few-shot examples), so any Step 8 schema tweak has to
update the prompts too.

Returned dicts are JSON-serialisable plain dicts (not pydantic
models). LangChain wraps each tool's output as a ToolMessage string;
introducing pydantic models here would force serialisation round-trips
without buying us validation that we can't already get from the
agent's structured-output mode on the FINAL response.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

# ── read_billing_rate ────────────────────────────────────────────────


@tool
def read_billing_rate(engagement_id: str, control_id: str, quarter: str) -> dict[str, Any]:
    """Look up the billing rate the auditor recorded for a
    (engagement, control, quarter) triple.

    Used by the Extraction sub-agent on the DC-9.D billing-rate-change
    path to anchor the rate-delta computation. Call once with the
    current quarter and once with the prior quarter, then diff.

    Args:
        engagement_id: Engagement identifier (e.g., ``"eng-2025-alpha"``).
        control_id: SOX control identifier — ``"DC-9"`` for billing-rate path.
        quarter: Audit period (``"Q1"``..``"Q4"``).

    Returns:
        Dict with keys:
            - ``rate`` (float | None): Recorded billing rate; None if absent.
            - ``rate_unit`` (str): e.g., ``"basis_points"`` or ``"usd_per_unit"``.
            - ``source_cell_ref`` (str): Workpaper cell ref for lineage.
            - ``recorded_at`` (str): ISO-8601 timestamp of the recording.
            - ``notes`` (str): Auditor note attached to the rate, if any.

    Placeholder: returns a canned empty payload (rate=None) until
    Step 8 swaps in the real ``silver.evidence`` reader.
    """
    # FIXME(step_08): Replace with real silver.evidence reader.
    # The reader will: lookup the row keyed by
    # (engagement_id, control_id, quarter), pull the DC-9.D attribute
    # check, and project the recorded rate + lineage. Until then this
    # stub returns the shape the agent prompt expects so the ReAct
    # loop can still progress in tests.
    return {
        "rate": None,
        "rate_unit": "basis_points",
        "source_cell_ref": f"<placeholder:{engagement_id}/{control_id}/{quarter}>",
        "recorded_at": "",
        "notes": "Step 8 placeholder — real silver.evidence reader pending.",
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
]
