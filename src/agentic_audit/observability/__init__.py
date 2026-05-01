"""Observability for the agentic audit framework.

Step 4 ships a thin `@traced_function` stub that emits structured INFO
log records on entry / exit with span name, trace id, duration, and
status. Step 9 will replace this with full OpenTelemetry spans +
exporter wiring; the public surface (`@traced_function(name=...)`)
stays the same so callers don't have to change.
"""

from agentic_audit.observability.decorators import traced_function

__all__ = ["traced_function"]
