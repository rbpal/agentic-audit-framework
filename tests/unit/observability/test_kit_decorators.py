"""Unit tests for `ai_ops_kit.decorators`.

Covers all three decorators (`traced_tool`, `traced_agent`, `traced_llm_call`)
across sync + async paths, success + exception cases, name override, args/
result truncation, and return-value attribute extraction.

The module-level ``_TRACER`` in ``ai_ops_kit.decorators`` is monkeypatched
to a test provider with ``InMemorySpanExporter``, so each test inspects
spans directly rather than going through the global OTel TracerProvider
(which can only be set once per process).
"""

from __future__ import annotations

from typing import Any

import pytest
from ai_ops_kit.decorators import (
    MAX_ATTR_CHARS,
    traced_agent,
    traced_llm_call,
    traced_tool,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

# ---------- fixtures ------------------------------------------------------


@pytest.fixture
def in_memory_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Swap the decorators' module-level tracer for one routed to in-memory export."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr("ai_ops_kit.decorators._TRACER", test_tracer)
    return exporter


def _only_span(exporter: InMemorySpanExporter) -> Any:
    """Assert exactly one span was emitted and return it."""
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"expected 1 span, got {len(spans)}"
    return spans[0]


# ---------- traced_tool — sync success path -------------------------------


def test_traced_tool_sync_emits_span_with_function_name(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    @traced_tool()
    def read_billing_rate(scenario_id: str) -> dict:
        return {"rate": 28.5, "scenario_id": scenario_id}

    read_billing_rate("DC-9.A")
    span = _only_span(in_memory_exporter)
    assert span.name == "tool.read_billing_rate"
    assert span.status.status_code == StatusCode.UNSET  # success defaults to UNSET in OTel


def test_traced_tool_sync_captures_args_kwargs_result_duration_success(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    @traced_tool()
    def add(a: int, b: int = 0) -> int:
        return a + b

    add(2, b=3)
    span = _only_span(in_memory_exporter)
    attrs = span.attributes
    # Arguments captured as JSON string {"args": [2], "kwargs": {"b": 3}}
    assert "tool.arguments" in attrs
    assert '"args": [2]' in attrs["tool.arguments"]
    assert '"b": 3' in attrs["tool.arguments"]
    # Result captured as JSON
    assert attrs["tool.result"] == "5"
    # Duration is a non-negative float
    assert isinstance(attrs["tool.duration_ms"], float)
    assert attrs["tool.duration_ms"] >= 0
    assert attrs["tool.success"] is True


def test_traced_tool_explicit_name_overrides_function_name(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    @traced_tool(tool_name="custom_tool")
    def whatever() -> None:
        return None

    whatever()
    assert _only_span(in_memory_exporter).name == "tool.custom_tool"


# ---------- traced_tool — async success path ------------------------------


@pytest.mark.asyncio
async def test_traced_tool_async_emits_span(in_memory_exporter: InMemorySpanExporter) -> None:
    @traced_tool()
    async def async_fetch(value: int) -> int:
        return value * 2

    result = await async_fetch(21)
    assert result == 42
    span = _only_span(in_memory_exporter)
    assert span.name == "tool.async_fetch"
    assert span.attributes["tool.success"] is True
    assert span.attributes["tool.result"] == "42"


# ---------- traced_tool — exception path ----------------------------------


def test_traced_tool_sync_records_exception_and_reraises(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    class _DeliberateError(RuntimeError):
        pass

    @traced_tool()
    def doomed() -> None:
        raise _DeliberateError("by design")

    with pytest.raises(_DeliberateError):
        doomed()

    span = _only_span(in_memory_exporter)
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["tool.success"] is False
    # OTel records the exception as an event on the span
    exception_events = [e for e in span.events if e.name == "exception"]
    assert len(exception_events) == 1
    assert exception_events[0].attributes["exception.type"].endswith("_DeliberateError")


@pytest.mark.asyncio
async def test_traced_tool_async_records_exception_and_reraises(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    @traced_tool()
    async def doomed_async() -> None:
        raise ValueError("async boom")

    with pytest.raises(ValueError, match="async boom"):
        await doomed_async()

    span = _only_span(in_memory_exporter)
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["tool.success"] is False


# ---------- traced_tool — truncation --------------------------------------


def test_traced_tool_truncates_large_result(in_memory_exporter: InMemorySpanExporter) -> None:
    big_string = "x" * (MAX_ATTR_CHARS * 2)

    @traced_tool()
    def big() -> str:
        return big_string

    big()
    result_attr = _only_span(in_memory_exporter).attributes["tool.result"]
    # Truncated value should be at the cap and end with the truncation marker
    assert len(result_attr) == MAX_ATTR_CHARS
    assert result_attr.endswith("...[truncated]")


def test_traced_tool_handles_non_json_serializable_args(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    class _NotJSONable:
        pass

    @traced_tool()
    def receives(obj: _NotJSONable) -> str:
        return "ok"

    receives(_NotJSONable())
    span = _only_span(in_memory_exporter)
    # Should not raise; falls back to repr / default=str. Just confirm attr is set.
    assert "tool.arguments" in span.attributes


# ---------- traced_agent --------------------------------------------------


def test_traced_agent_emits_span_with_agent_prefix(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    @traced_agent()
    def extraction_agent() -> dict:
        return {"iterations": 3, "tools_called": ["read_billing_rate"], "total_tokens": 1234}

    extraction_agent()
    span = _only_span(in_memory_exporter)
    assert span.name == "agent.extraction_agent"
    assert span.attributes["agent.success"] is True
    assert span.attributes["agent.iterations"] == 3
    # Non-primitive values get JSON-serialised
    assert span.attributes["agent.tools_called"] == '["read_billing_rate"]'
    assert span.attributes["agent.total_tokens"] == 1234


def test_traced_agent_does_not_capture_args(in_memory_exporter: InMemorySpanExporter) -> None:
    """Agent decorator deliberately skips args/result capture — they could be huge."""

    @traced_agent()
    def agent_with_args(state: dict) -> dict:
        return {"iterations": 1}

    agent_with_args({"big": "state"})
    span = _only_span(in_memory_exporter)
    assert "agent.arguments" not in span.attributes
    assert "agent.result" not in span.attributes


def test_traced_agent_no_extraction_when_return_is_not_dict(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    @traced_agent()
    def returns_string() -> str:
        return "not a dict"

    returns_string()
    span = _only_span(in_memory_exporter)
    # Standard attrs still set
    assert span.attributes["agent.success"] is True
    # No return-value-extracted attrs
    for key in ("agent.iterations", "agent.tools_called", "agent.total_tokens"):
        assert key not in span.attributes


# ---------- traced_llm_call -----------------------------------------------


def test_traced_llm_call_extracts_known_token_and_cost_fields(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    @traced_llm_call(model="gpt-4o")
    def call_llm() -> dict:
        return {
            "text": "hello",
            "prompt_tokens": 120,
            "completion_tokens": 45,
            "total_cost_usd": 0.0023,
            "model_version": "gpt-4o-2024-08-06",
            "response_time_ms": 412.5,
        }

    call_llm()
    span = _only_span(in_memory_exporter)
    assert span.name == "llm.gpt-4o"
    assert span.attributes["llm.prompt_tokens"] == 120
    assert span.attributes["llm.completion_tokens"] == 45
    assert span.attributes["llm.total_cost_usd"] == 0.0023
    assert span.attributes["llm.model_version"] == "gpt-4o-2024-08-06"
    assert span.attributes["llm.response_time_ms"] == 412.5


def test_traced_llm_call_uses_function_name_when_no_model_arg(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    @traced_llm_call()
    def default_named_call() -> dict:
        return {}

    default_named_call()
    assert _only_span(in_memory_exporter).name == "llm.default_named_call"


def test_traced_llm_call_records_exception(in_memory_exporter: InMemorySpanExporter) -> None:
    @traced_llm_call(model="gpt-4o")
    def llm_fail() -> dict:
        raise TimeoutError("upstream timeout")

    with pytest.raises(TimeoutError, match="upstream timeout"):
        llm_fail()

    span = _only_span(in_memory_exporter)
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["llm.success"] is False
