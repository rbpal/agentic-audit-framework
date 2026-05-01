"""`@traced_function` decorator stub for Layer 1.

Step 4 deliverable. Emits structured INFO log records on function
entry / exit with `span`, `trace_id`, `duration_ms`, `status` (and
`error` on the failure path). Records go through the stdlib `logging`
module on the `agentic_audit.trace` logger so tests can capture them
via pytest's `caplog` fixture without pulling in `structlog` or
`opentelemetry`.

Step 9 will replace this with real OpenTelemetry spans + exporter
config + `trace_id` propagation across processes. The public surface
— `@traced_function(name="...")` — stays stable across that swap so
callers (BronzeReader.read, check_attribute, extract,
SilverWriter.write_evidence) don't have to change.

Decorator-stacking note: when applied above `@retry`, one span wraps
all retry attempts (one method invocation = one trace). When applied
below `@retry`, each retry attempt is a separate span. Layer 1 puts
`@traced_function` outermost (above `@retry`) so the caller sees one
span per public method call regardless of internal retries.
"""

from __future__ import annotations

import logging
import time
import uuid
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable


_log = logging.getLogger("agentic_audit.trace")

T = TypeVar("T")


def traced_function(name: str | None = None) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Wrap a function so each call emits structured `span_start` /
    `span_end` log records.

    `name` overrides the auto-derived span name (`{module}.{qualname}`).
    Pass an explicit name for layer-stable identifiers like
    ``"layer1.bronze_reader.read"`` so the span name doesn't change
    when the function moves between modules.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        span_name = name or f"{fn.__module__}.{fn.__qualname__}"

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            trace_id = uuid.uuid4().hex
            t0 = time.perf_counter()
            _log.info(
                "span_start",
                extra={"span": span_name, "trace_id": trace_id},
            )
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                _log.error(
                    "span_end",
                    extra={
                        "span": span_name,
                        "trace_id": trace_id,
                        "duration_ms": (time.perf_counter() - t0) * 1000.0,
                        "status": "error",
                        "error": str(exc),
                    },
                )
                raise
            _log.info(
                "span_end",
                extra={
                    "span": span_name,
                    "trace_id": trace_id,
                    "duration_ms": (time.perf_counter() - t0) * 1000.0,
                    "status": "ok",
                },
            )
            return result

        return wrapper

    return decorator


__all__ = ["traced_function"]
