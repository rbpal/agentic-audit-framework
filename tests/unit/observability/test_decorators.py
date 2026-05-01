"""Unit tests for `agentic_audit.observability.decorators.traced_function`.

Verifies the stub emits structured `span_start` / `span_end` log records
on the `agentic_audit.trace` logger with `span`, `trace_id`,
`duration_ms`, and `status` (plus `error` on the failure path). Captures
records via pytest's `caplog` fixture; no `structlog` or
`opentelemetry` involved at this layer.
"""

from __future__ import annotations

import logging

import pytest

from agentic_audit.observability import traced_function

# ---------- happy path ---------------------------------------------------


def test_traced_function_emits_start_and_end_on_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @traced_function("layer1.test.happy")
    def add(a: int, b: int) -> int:
        return a + b

    with caplog.at_level(logging.INFO, logger="agentic_audit.trace"):
        assert add(2, 3) == 5

    msgs = [r.message for r in caplog.records]
    assert msgs == ["span_start", "span_end"]
    start, end = caplog.records
    assert start.span == "layer1.test.happy"  # type: ignore[attr-defined]
    assert end.span == "layer1.test.happy"  # type: ignore[attr-defined]
    assert start.trace_id == end.trace_id  # type: ignore[attr-defined]
    assert end.status == "ok"  # type: ignore[attr-defined]
    assert end.duration_ms >= 0.0  # type: ignore[attr-defined]


def test_traced_function_default_span_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When no name is supplied, span derives from `{module}.{qualname}`."""

    @traced_function()
    def my_func() -> str:
        return "hi"

    with caplog.at_level(logging.INFO, logger="agentic_audit.trace"):
        my_func()

    span_name = caplog.records[0].span  # type: ignore[attr-defined]
    assert "my_func" in span_name
    assert "test_decorators" in span_name


def test_traced_function_each_call_gets_unique_trace_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @traced_function("layer1.test.unique")
    def noop() -> None:
        return None

    with caplog.at_level(logging.INFO, logger="agentic_audit.trace"):
        noop()
        noop()

    trace_ids = {r.trace_id for r in caplog.records}  # type: ignore[attr-defined]
    assert len(trace_ids) == 2  # 2 calls × 1 unique id each (start+end share within a call)


def test_traced_function_passes_args_through(caplog: pytest.LogCaptureFixture) -> None:
    @traced_function("layer1.test.args")
    def concat(*parts: str, sep: str = "-") -> str:
        return sep.join(parts)

    with caplog.at_level(logging.INFO, logger="agentic_audit.trace"):
        assert concat("a", "b", "c", sep="_") == "a_b_c"


# ---------- error path ---------------------------------------------------


def test_traced_function_emits_error_status_when_inner_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @traced_function("layer1.test.boom")
    def boom() -> None:
        raise RuntimeError("kaboom")

    with (
        caplog.at_level(logging.INFO, logger="agentic_audit.trace"),
        pytest.raises(RuntimeError, match="kaboom"),
    ):
        boom()

    msgs = [r.message for r in caplog.records]
    assert msgs == ["span_start", "span_end"]
    end = caplog.records[1]
    assert end.status == "error"  # type: ignore[attr-defined]
    assert end.error == "kaboom"  # type: ignore[attr-defined]
    # span_end on the error path is logged at ERROR level, not INFO
    assert end.levelno == logging.ERROR
