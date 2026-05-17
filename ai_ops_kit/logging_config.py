"""Structured JSON logging for ai_ops_kit.

``configure_logging`` sets up ``structlog`` to emit one JSON record per log
line, with two cross-cutting concerns baked into the processor chain:

- **OTel trace-context injection** — ``add_trace_context`` reads the
  currently-active span (set by ``trace_context`` or any OTel span
  context manager) and injects ``trace_id`` / ``span_id`` into every
  record. Lets you correlate a log line back to its span in
  Application Insights / Azure Monitor.

- **Sensitive-field redaction** — ``_make_redact_processor`` masks the
  values of any top-level keys matching a known-sensitive name
  (``api_key``, ``password``, ``token``, …). Match is case-insensitive.
  Callers can extend the list via ``extra_sensitive_keys``.

Output is single-line JSON per record, parseable by Application Insights
log queries and Azure Monitor workbook KQL.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog
from opentelemetry import trace

# Top-level field names whose values are masked in log output. Match is
# case-insensitive (``API_KEY``, ``Authorization``, etc. all redacted).
# Pass ``extra_sensitive_keys`` to ``configure_logging`` for domain-specific
# additions (e.g. ``{"databricks_token", "azure_subscription_id"}``).
DEFAULT_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "password",
        "passwd",
        "pwd",
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "client_secret",
        "authorization",
        "auth",
        "bearer",
        "cookie",
        "session",
        "session_id",
        "private_key",
        "ssh_key",
        "credential",
        "credentials",
    }
)

_REDACTION_MASK = "***"


def add_trace_context(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Inject ``trace_id`` and ``span_id`` from the active OTel span.

    No-op when no span is active or the active span has an invalid context
    (e.g., logging outside any traced section). IDs are formatted as the
    OTel-standard hex strings: 32-char ``trace_id``, 16-char ``span_id``.
    """
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return event_dict
    event_dict["trace_id"] = format(ctx.trace_id, "032x")
    event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def _make_redact_processor(sensitive_keys: frozenset[str]) -> Any:
    """Build a structlog processor that masks values of sensitive top-level keys.

    The closure captures the resolved key set so callers can extend
    ``DEFAULT_SENSITIVE_KEYS`` without mutating the module-level constant.
    """

    def redact_sensitive(
        _logger: Any, _method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        for key in list(event_dict.keys()):
            if key.lower() in sensitive_keys:
                event_dict[key] = _REDACTION_MASK
        return event_dict

    return redact_sensitive


def configure_logging(
    log_level: str = "INFO",
    extra_sensitive_keys: frozenset[str] | set[str] | None = None,
) -> None:
    """Configure structlog with JSON output, OTel trace injection, and redaction.

    Args:
        log_level: One of ``"DEBUG"``, ``"INFO"``, ``"WARNING"``, ``"ERROR"``,
            ``"CRITICAL"``. Applied at the stdlib level too so structlog
            respects the filter.
        extra_sensitive_keys: Domain-specific keys to add to the redaction
            list (e.g., ``{"databricks_token", "azure_subscription_id"}``).
            Merged with ``DEFAULT_SENSITIVE_KEYS``.

    Processor chain (in order):
        1. ``add_log_level`` — injects ``level`` field
        2. ``TimeStamper`` — injects ISO-8601 ``timestamp`` field
        3. ``add_trace_context`` — injects ``trace_id`` / ``span_id`` when a
           span is active
        4. ``redact_sensitive`` — masks sensitive field values
        5. ``JSONRenderer`` — final string output
    """
    sensitive = DEFAULT_SENSITIVE_KEYS
    if extra_sensitive_keys:
        sensitive = sensitive | frozenset(extra_sensitive_keys)

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            add_trace_context,
            _make_redact_processor(sensitive),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    # Set up stdlib root so structlog records actually emit. basicConfig is
    # a no-op when the root already has handlers (e.g., pytest's caplog), so
    # we don't yank rugs out from under existing handlers. Set the level
    # separately to ensure the requested filter applies either way.
    logging.basicConfig(level=log_level, format="%(message)s")
    logging.getLogger().setLevel(log_level)
