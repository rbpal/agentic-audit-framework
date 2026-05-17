"""OpenTelemetry-instrumented decorators for AI workloads.

Three decorators sharing a common lifecycle (start span → capture metadata →
record success/exception → set duration_ms):

- ``@traced_tool(tool_name=None)`` — for tool functions. Captures
  ``tool.arguments`` (JSON-truncated), ``tool.result`` (JSON-truncated at
  10 KB), ``tool.duration_ms``, ``tool.success``.
- ``@traced_agent(agent_name=None)`` — for agent functions. Captures
  ``agent.duration_ms``, ``agent.success``. If the function returns a dict
  with keys ``iterations`` / ``tools_called`` / ``total_tokens``, those are
  surfaced as ``agent.{key}`` attributes.
- ``@traced_llm_call(model=None)`` — for LLM call functions. Captures
  ``llm.duration_ms``, ``llm.success``. If the function returns a dict with
  known LLM fields (``prompt_tokens``, ``completion_tokens``, ``total_cost_usd``,
  etc.), those become ``llm.{key}`` attributes.

All three decorators support both sync and async functions (inspected via
``inspect.iscoroutinefunction``). On exception: ``<prefix>.success`` is set
to ``False``, duration is captured, and the exception is re-raised. OTel's
``start_as_current_span`` context manager records the exception and sets
span status to ERROR automatically on exit, so this code intentionally does
not call ``record_exception`` / ``set_status`` (doing so would duplicate
the recorded event).

Args and results are JSON-serialized with a 10 KB cap; non-serializable
values fall back to ``repr()``. Callers may also set custom attributes inside
the function body via ``trace.get_current_span().set_attribute(...)``.
"""

from __future__ import annotations

import functools
import inspect
import json
import time
from collections.abc import Callable
from typing import Any, TypeVar

from opentelemetry import trace

F = TypeVar("F", bound=Callable[..., Any])

# Max characters for JSON-serialized span attributes. ~10 KB is a sensible
# default per OTel guidance: most backends silently drop oversized attrs.
MAX_ATTR_CHARS = 10240

# Return-value fields each decorator type knows about. If the decorated
# function returns a dict, these keys (when present) become span attributes.
_AGENT_RETURN_KEYS = frozenset({"iterations", "tools_called", "total_tokens"})
_LLM_RETURN_KEYS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "total_cost_usd",
        "model_version",
        "response_time_ms",
    }
)

_TRACER = trace.get_tracer("ai_ops_kit.decorators")
_TRUNCATION_SUFFIX = "...[truncated]"


def _to_json_truncated(obj: Any, max_chars: int = MAX_ATTR_CHARS) -> str:
    """JSON-serialize ``obj`` with size cap. Falls back to ``repr`` if not serialisable."""
    try:
        s = json.dumps(obj, default=str)
    except (TypeError, ValueError):
        s = repr(obj)
    if len(s) > max_chars:
        return s[: max_chars - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX
    return s


def _extract_dict_attrs(result: Any, known_keys: frozenset[str], prefix: str) -> dict[str, Any]:
    """If ``result`` is a dict, return ``{prefix.k: v}`` for known keys present."""
    if not isinstance(result, dict):
        return {}
    return {f"{prefix}.{k}": result[k] for k in known_keys if k in result}


def _set_attr(span: trace.Span, key: str, value: Any) -> None:
    """Set a span attribute; serialize complex types so OTel accepts the value."""
    if isinstance(value, (str, int, float, bool)):
        span.set_attribute(key, value)
    else:
        span.set_attribute(key, _to_json_truncated(value))


def _record_success(
    span: trace.Span,
    result: Any,
    start: float,
    prefix: str,
    capture_args: bool,
    return_value_keys: frozenset[str],
) -> None:
    """Common success-path finalization shared by sync and async wrappers."""
    span.set_attribute(f"{prefix}.duration_ms", (time.perf_counter() - start) * 1000)
    span.set_attribute(f"{prefix}.success", True)
    if capture_args:
        span.set_attribute(f"{prefix}.result", _to_json_truncated(result))
    for key, value in _extract_dict_attrs(result, return_value_keys, prefix).items():
        _set_attr(span, key, value)


def _record_error(span: trace.Span, start: float, prefix: str) -> None:
    """Common error-path finalization shared by sync and async wrappers.

    Only sets our custom attrs (duration_ms, success). OTel's
    ``start_as_current_span`` context manager handles ``set_status(ERROR)``
    and ``record_exception`` automatically when the exception propagates
    out, so doing it here would duplicate the event.
    """
    span.set_attribute(f"{prefix}.duration_ms", (time.perf_counter() - start) * 1000)
    span.set_attribute(f"{prefix}.success", False)


def _make_decorator(
    prefix: str,
    *,
    capture_args: bool,
    return_value_keys: frozenset[str] = frozenset(),
) -> Callable[..., Callable[[F], F]]:
    """Build a decorator factory parameterised by span-attribute prefix and capture mode."""

    def decorator_factory(name: str | None = None) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            resolved_name = name or func.__name__
            span_name = f"{prefix}.{resolved_name}"

            if inspect.iscoroutinefunction(func):

                @functools.wraps(func)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    with _TRACER.start_as_current_span(span_name) as span:
                        start = time.perf_counter()
                        if capture_args:
                            span.set_attribute(
                                f"{prefix}.arguments",
                                _to_json_truncated({"args": list(args), "kwargs": kwargs}),
                            )
                        try:
                            result = await func(*args, **kwargs)
                        except BaseException:
                            _record_error(span, start, prefix)
                            raise
                        _record_success(
                            span, result, start, prefix, capture_args, return_value_keys
                        )
                        return result

                return async_wrapper  # type: ignore[return-value]

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                with _TRACER.start_as_current_span(span_name) as span:
                    start = time.perf_counter()
                    if capture_args:
                        span.set_attribute(
                            f"{prefix}.arguments",
                            _to_json_truncated({"args": list(args), "kwargs": kwargs}),
                        )
                    try:
                        result = func(*args, **kwargs)
                    except BaseException:
                        _record_error(span, start, prefix)
                        raise
                    _record_success(span, result, start, prefix, capture_args, return_value_keys)
                    return result

            return sync_wrapper  # type: ignore[return-value]

        return decorator

    return decorator_factory


# Public decorators ---------------------------------------------------------
#
# Each public decorator is a thin wrapper around ``_make_decorator`` so that:
# (a) the keyword-argument names match the plan signatures
#     (``tool_name`` / ``agent_name`` / ``model`` — NOT a generic ``name``);
# (b) docstrings work via the standard ``def`` syntax instead of needing
#     manual ``__doc__`` assignment.


def traced_tool(tool_name: str | None = None) -> Callable[[F], F]:
    """Instrument a tool function. Captures arguments, result, duration, success.

    Span name is ``tool.<tool_name>`` (or ``tool.<func.__name__>`` if no
    override). On success, sets ``tool.arguments``, ``tool.result``
    (JSON-truncated at 10 KB each), ``tool.duration_ms``, and
    ``tool.success=True``. On exception, sets ``tool.success=False``, marks
    the span ERROR, records the exception, and re-raises.

    Works for both sync and async functions::

        @traced_tool()
        def read_billing_rate(scenario_id: str) -> dict:
            ...

        @traced_tool(tool_name="custom_name")
        async def some_tool(arg: int) -> int:
            ...
    """
    return _make_decorator("tool", capture_args=True)(tool_name)


def traced_agent(agent_name: str | None = None) -> Callable[[F], F]:
    """Instrument an agent function. Captures duration + success.

    Span name is ``agent.<agent_name>`` (or ``agent.<func.__name__>``). If
    the function returns a dict containing ``iterations``, ``tools_called``,
    or ``total_tokens``, those become ``agent.{key}`` span attributes
    (non-primitive values are JSON-serialised).

    Args/result are intentionally NOT captured (agent state can be very
    large; rely on tool-level capture or set custom attrs in the function
    body via ``trace.get_current_span().set_attribute(...)``)::

        @traced_agent()
        def extraction_agent(state: AgentState) -> dict:
            ...
            return {"iterations": 3, "tools_called": ["read_billing_rate"], "total_tokens": 1234}
    """
    return _make_decorator(
        "agent",
        capture_args=False,
        return_value_keys=_AGENT_RETURN_KEYS,
    )(agent_name)


def traced_llm_call(model: str | None = None) -> Callable[[F], F]:
    """Instrument an LLM call. Captures duration + success.

    Span name is ``llm.<model>`` (or ``llm.<func.__name__>``). If the
    function returns a dict containing ``prompt_tokens``,
    ``completion_tokens``, ``total_tokens``, ``total_cost_usd``,
    ``model_version``, or ``response_time_ms``, those become ``llm.{key}``
    span attributes.

    Args/result are intentionally NOT captured (prompts can be huge; set
    custom attrs in the function body or pass token counts via the
    returned dict)::

        @traced_llm_call(model="gpt-4o")
        def call_llm(messages: list) -> dict:
            ...
            return {"text": "...", "prompt_tokens": 120, "completion_tokens": 45, "total_cost_usd": 0.002}
    """
    return _make_decorator(
        "llm",
        capture_args=False,
        return_value_keys=_LLM_RETURN_KEYS,
    )(model)
