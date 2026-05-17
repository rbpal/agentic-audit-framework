"""ai_ops_kit — shared OpenTelemetry tracer, structured logging, and agent decorators.

Consumed by Project 1 (agentic-audit-framework) directly from this directory, and by
Projects 2 and 3 as a git submodule. Designed to be self-contained: must NOT import
from any project-specific code (enforced by CI check planned in step_09_task_05).
"""

from .decorators import traced_agent, traced_llm_call, traced_tool
from .logging_config import configure_logging
from .tracing import get_tracer, init_tracer, trace_context

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "configure_logging",
    "get_tracer",
    "init_tracer",
    "trace_context",
    "traced_agent",
    "traced_llm_call",
    "traced_tool",
]
