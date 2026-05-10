"""Validation tests for ``agentic_audit.models.telemetry``.

Three small models — ``CallUsage``, ``UsageRecorder``, ``CostTelemetry`` —
plus the ``estimate_cost_usd`` helper. Every invariant the sweep
driver depends on is pinned here so refactors can't silently break
the cost-telemetry contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agentic_audit.models.telemetry import (
    MODEL_PRICING_USD_PER_1K,
    CallUsage,
    CostTelemetry,
    UsageRecorder,
    estimate_cost_usd,
)

UTC_TS = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)


# ---------- CallUsage --------------------------------------------------


def test_call_usage_total_is_sum_of_prompt_and_completion() -> None:
    usage = CallUsage(prompt_tokens=120, completion_tokens=80)
    assert usage.total_tokens == 200


def test_call_usage_rejects_negative_prompt_tokens() -> None:
    with pytest.raises(ValidationError):
        CallUsage(prompt_tokens=-1, completion_tokens=0)


def test_call_usage_rejects_negative_completion_tokens() -> None:
    with pytest.raises(ValidationError):
        CallUsage(prompt_tokens=0, completion_tokens=-1)


def test_call_usage_zero_zero_is_valid() -> None:
    """Empty-content responses still represent a billed API call (rare
    but possible). Zero counts must be accepted, not rejected as
    'no usage'."""
    usage = CallUsage(prompt_tokens=0, completion_tokens=0)
    assert usage.total_tokens == 0


# ---------- UsageRecorder ----------------------------------------------


def test_usage_recorder_starts_at_zero() -> None:
    recorder = UsageRecorder()
    assert recorder.prompt_tokens == 0
    assert recorder.completion_tokens == 0
    assert recorder.n_calls == 0
    assert recorder.total_tokens == 0


def test_usage_recorder_record_accumulates_across_calls() -> None:
    """Three calls of (100, 50), (200, 80), (300, 120) — totals
    must reflect the sum, not the last call."""
    recorder = UsageRecorder()
    recorder.record(CallUsage(prompt_tokens=100, completion_tokens=50))
    recorder.record(CallUsage(prompt_tokens=200, completion_tokens=80))
    recorder.record(CallUsage(prompt_tokens=300, completion_tokens=120))

    assert recorder.prompt_tokens == 600
    assert recorder.completion_tokens == 250
    assert recorder.total_tokens == 850
    assert recorder.n_calls == 3


def test_usage_recorder_snapshot_matches_running_totals() -> None:
    recorder = UsageRecorder()
    recorder.record(CallUsage(prompt_tokens=10, completion_tokens=5))
    recorder.record(CallUsage(prompt_tokens=20, completion_tokens=10))

    snap = recorder.snapshot()
    assert snap.prompt_tokens == 30
    assert snap.completion_tokens == 15
    assert snap.total_tokens == 45


def test_usage_recorder_snapshot_does_not_freeze_recorder() -> None:
    """Calling snapshot() returns an immutable view but does NOT
    freeze the recorder — further record() calls keep accumulating."""
    recorder = UsageRecorder()
    recorder.record(CallUsage(prompt_tokens=10, completion_tokens=5))

    first_snap = recorder.snapshot()
    assert first_snap.total_tokens == 15

    recorder.record(CallUsage(prompt_tokens=100, completion_tokens=50))
    second_snap = recorder.snapshot()
    assert second_snap.total_tokens == 165
    # The original snapshot is unchanged (immutable Pydantic view)
    assert first_snap.total_tokens == 15


def test_usage_recorder_rejects_negative_initial_state() -> None:
    """Constructing a recorder with negative counters is nonsense and
    must be rejected at construction time."""
    with pytest.raises(ValidationError):
        UsageRecorder(prompt_tokens=-1)
    with pytest.raises(ValidationError):
        UsageRecorder(completion_tokens=-1)
    with pytest.raises(ValidationError):
        UsageRecorder(n_calls=-1)


# ---------- estimate_cost_usd ------------------------------------------


def test_estimate_cost_usd_known_model_gpt4o() -> None:
    """gpt-4o pricing: $0.0025 / 1k input, $0.0100 / 1k output.
    1000 input + 500 output = $0.0025 + $0.0050 = $0.0075."""
    cost = estimate_cost_usd(deployment="gpt-4o", input_tokens=1000, output_tokens=500)
    assert cost == pytest.approx(0.0075)


def test_estimate_cost_usd_known_model_gpt4o_mini() -> None:
    """gpt-4o-mini pricing: $0.000150 / 1k input, $0.000600 / 1k output."""
    cost = estimate_cost_usd(deployment="gpt-4o-mini", input_tokens=1000, output_tokens=500)
    assert cost == pytest.approx(0.000150 + 0.000300)


def test_estimate_cost_usd_zero_tokens_is_zero() -> None:
    cost = estimate_cost_usd(deployment="gpt-4o", input_tokens=0, output_tokens=0)
    assert cost == 0.0


def test_estimate_cost_usd_unknown_model_returns_none() -> None:
    """A deployment that's not in ``MODEL_PRICING_USD_PER_1K`` must
    return ``None`` (not zero, not raise) so the caller can log a
    WARN and persist a NULL ``cost_usd`` rather than dropping the row."""
    cost = estimate_cost_usd(deployment="gpt-5-future", input_tokens=1000, output_tokens=500)
    assert cost is None


def test_pricing_table_has_gpt_4o_baseline() -> None:
    """gpt-4o is the production deployment as of step_05. Removing it
    from the pricing table breaks every sweep silently — pin it."""
    assert "gpt-4o" in MODEL_PRICING_USD_PER_1K
    input_per_1k, output_per_1k = MODEL_PRICING_USD_PER_1K["gpt-4o"]
    assert input_per_1k > 0
    assert output_per_1k > 0
    # Output should cost more than input (well-known LLM-pricing shape;
    # if this flips, the price table almost certainly has a typo).
    assert output_per_1k > input_per_1k


# ---------- CostTelemetry ----------------------------------------------


def _make_telemetry(**overrides: object) -> CostTelemetry:
    defaults: dict[str, object] = {
        "agent_run_id": "SWEEP-2026-05-09-001",
        "input_tokens": 10_000,
        "output_tokens": 4_000,
        "total_tokens": 14_000,
        "latency_ms": 120_000,
        "cost_usd": 0.075,
        "model_version": "gpt-4o",
        "started_at": UTC_TS,
        "completed_at": UTC_TS + timedelta(minutes=2),
    }
    defaults.update(overrides)
    return CostTelemetry(**defaults)  # type: ignore[arg-type]


def test_cost_telemetry_happy_path() -> None:
    t = _make_telemetry()
    assert t.agent_run_id == "SWEEP-2026-05-09-001"
    assert t.total_tokens == 14_000
    assert t.cost_usd == pytest.approx(0.075)
    assert t.model_version == "gpt-4o"


def test_cost_telemetry_total_must_equal_input_plus_output() -> None:
    """Drift between scalar totals and the underlying counts would
    corrupt cost dashboards. The model_validator catches it at
    construction time."""
    with pytest.raises(ValidationError, match="total_tokens must equal"):
        _make_telemetry(input_tokens=10_000, output_tokens=4_000, total_tokens=99_999)


def test_cost_telemetry_completed_must_be_after_started() -> None:
    """A clock-skew row would corrupt latency derivations. Reject."""
    with pytest.raises(ValidationError, match="completed_at"):
        _make_telemetry(
            started_at=UTC_TS + timedelta(minutes=2),
            completed_at=UTC_TS,
        )


def test_cost_telemetry_completed_equal_started_is_valid() -> None:
    """Edge case: zero-elapsed sweep (e.g., empty engagement, no
    LLM calls). Should still construct."""
    t = _make_telemetry(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        latency_ms=0,
        cost_usd=0.0,
        started_at=UTC_TS,
        completed_at=UTC_TS,
    )
    assert t.latency_ms == 0


def test_cost_telemetry_cost_usd_can_be_none() -> None:
    """An unknown-deployment sweep persists token + latency data
    with NULL cost_usd rather than dropping the row."""
    t = _make_telemetry(cost_usd=None)
    assert t.cost_usd is None


def test_cost_telemetry_rejects_empty_agent_run_id() -> None:
    with pytest.raises(ValidationError):
        _make_telemetry(agent_run_id="")


def test_cost_telemetry_rejects_empty_model_version() -> None:
    with pytest.raises(ValidationError):
        _make_telemetry(model_version="")


def test_cost_telemetry_rejects_negative_tokens() -> None:
    with pytest.raises(ValidationError):
        _make_telemetry(input_tokens=-1, total_tokens=-1 + 4_000)


def test_cost_telemetry_rejects_negative_latency() -> None:
    with pytest.raises(ValidationError):
        _make_telemetry(latency_ms=-1)


def test_cost_telemetry_rejects_negative_cost() -> None:
    with pytest.raises(ValidationError):
        _make_telemetry(cost_usd=-0.01)
