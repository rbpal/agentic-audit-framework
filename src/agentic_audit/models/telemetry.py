"""Cost telemetry models for Layer 2 sweeps.

Three small types own the boundary between LLM call accounting and the
``audit_dev.gold.cost_telemetry`` Delta table:

- ``CallUsage`` — per-LLM-call token counts, recorded by the generator
  immediately after each ``chat.completions.create`` call. Mirrors the
  ``response.usage`` shape Azure OpenAI returns.
- ``UsageRecorder`` — running accumulator. The generator holds a
  reference to one of these and calls ``record(...)`` after each LLM
  call; the sweep driver reads ``snapshot()`` at the end to build the
  per-run cost-telemetry row. A fresh recorder per sweep keeps grain
  alignment with ``agent_run_id``.
- ``CostTelemetry`` — exact column-for-column shape of one row in
  ``audit_dev.gold.cost_telemetry``. Built once per sweep and handed
  to ``CostTelemetryWriter`` for the MERGE.

``cost_usd`` is computed via ``MODEL_PRICING_USD_PER_1K`` (a constant
declared here, source-of-truth in code rather than the table). If a
deployment maps to no known model, ``cost_usd`` is set to ``None`` and
the sweep logs a WARN — the row still lands so token + latency data
isn't lost. See ``privateDocs/step_05_layer2_narrative.md`` follow-up
#1 for the design rationale (recorder-callback over tuple-return).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

# ---- Pricing -----------------------------------------------------------
#
# USD per 1,000 tokens, Azure OpenAI list pricing. Keyed by deployment
# name (the string passed to ``chat.completions.create(model=...)``).
# Source: https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/
# (gpt-4o-2024-08-06 row, captured 2026-05-09).
#
# When a new deployment lands (e.g. ``gpt-4o-mini``, ``gpt-4.1``), add
# its row here. Unknown deployments → ``cost_usd=None`` + WARN log;
# token counts and latency still persist.
MODEL_PRICING_USD_PER_1K: dict[str, tuple[float, float]] = {
    # deployment_name: (input_per_1k, output_per_1k)
    "gpt-4o": (0.0025, 0.0100),
    "gpt-4o-mini": (0.000150, 0.000600),
}


def estimate_cost_usd(
    *,
    deployment: str,
    input_tokens: int,
    output_tokens: int,
) -> float | None:
    """Compute USD cost from the price table; ``None`` if the deployment
    isn't in ``MODEL_PRICING_USD_PER_1K``.

    Caller is expected to log a WARN when this returns ``None`` so the
    operator notices a pricing-table gap (rather than a silent zero).
    """
    pricing = MODEL_PRICING_USD_PER_1K.get(deployment)
    if pricing is None:
        return None
    input_per_1k, output_per_1k = pricing
    return (input_tokens / 1000.0) * input_per_1k + (output_tokens / 1000.0) * output_per_1k


# ---- CallUsage ---------------------------------------------------------


class CallUsage(BaseModel):
    """Per-LLM-call token counts.

    Field names mirror Azure OpenAI's ``response.usage`` shape so the
    mapping is one-line at the call site:
    ``CallUsage(prompt_tokens=u.prompt_tokens, completion_tokens=u.completion_tokens)``.

    ``total_tokens`` is computed (not user-supplied) — keeps the
    invariant ``total == prompt + completion`` machine-checked.
    """

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# ---- UsageRecorder -----------------------------------------------------


class UsageRecorder(BaseModel):
    """Running accumulator for a sweep's LLM token spend.

    Mutable across ``record(...)`` calls — Pydantic's
    ``model_config = {"validate_assignment": True}`` would reject
    in-place increment, so this class uses plain instance attributes
    via ``__init__`` and exposes a small mutating API. The validation
    surface is at construction time only (counts start at zero, can't
    start negative).

    Snapshot via ``snapshot()`` to get an immutable view; the sweep
    builds the ``CostTelemetry`` row from that snapshot.

    A fresh recorder per sweep is the contract — re-using a recorder
    across sweeps would smear two ``agent_run_id`` cohorts into one
    telemetry row.
    """

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    n_calls: int = Field(default=0, ge=0)

    def record(self, usage: CallUsage) -> None:
        """Add one LLM call's tokens to the running totals."""
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.n_calls += 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def snapshot(self) -> CallUsage:
        """Immutable view of the running totals as a ``CallUsage``.

        Reusing the ``CallUsage`` shape here is intentional: the sweep
        accumulates per-call usage into the recorder, then treats the
        recorder's totals as a single aggregate ``CallUsage`` when
        building the telemetry row.
        """
        return CallUsage(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )


# ---- CostTelemetry -----------------------------------------------------


class CostTelemetry(BaseModel):
    """Exact shape of one row in ``audit_dev.gold.cost_telemetry``.

    Grain: one row per ``agent_run_id`` (== sweep ``generation_run_id``
    on the narrative side). Joinable to ``audit_dev.gold.narratives``
    via ``agent_run_id``.

    ``total_tokens`` is enforced to equal ``input_tokens +
    output_tokens`` — drifting denormalisation here would make the
    table lie and break cost dashboards. ``completed_at`` must be
    ``>= started_at``; a clock-skew row would corrupt ``latency_ms``
    derivations.

    ``cost_usd`` is nullable on purpose — see
    ``MODEL_PRICING_USD_PER_1K`` doc — so an unknown-deployment sweep
    still persists token + latency data.
    """

    agent_run_id: str = Field(min_length=1)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    latency_ms: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0)

    model_version: str = Field(min_length=1)

    started_at: datetime
    completed_at: datetime

    @model_validator(mode="after")
    def _totals_consistent(self) -> CostTelemetry:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError(
                "total_tokens must equal input_tokens + output_tokens "
                f"({self.input_tokens} + {self.output_tokens} != {self.total_tokens})"
            )
        if self.completed_at < self.started_at:
            raise ValueError(
                f"completed_at ({self.completed_at!s}) must be >= started_at ({self.started_at!s})"
            )
        return self


__all__ = [
    "MODEL_PRICING_USD_PER_1K",
    "CallUsage",
    "CostTelemetry",
    "UsageRecorder",
    "estimate_cost_usd",
]
