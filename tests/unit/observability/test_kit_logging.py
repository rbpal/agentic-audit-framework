"""Unit tests for `ai_ops_kit.logging_config`.

Covers three concerns:

- ``_make_redact_processor``: masks values of default + extra sensitive keys,
  case-insensitively, top-level only.
- ``add_trace_context``: injects ``trace_id`` / ``span_id`` from the active
  OTel span; no-op when no span active.
- ``configure_logging``: end-to-end JSON output via stdlib logging
  (captured with pytest ``caplog``); merges extra sensitive keys with the
  default set.

Each processor is also tested in isolation to keep failures localised
(integration tests can fail for many reasons; isolated tests pinpoint the
exact processor that regressed).
"""

from __future__ import annotations

import json

import pytest
import structlog
from ai_ops_kit.logging_config import (
    DEFAULT_SENSITIVE_KEYS,
    _make_redact_processor,
    add_trace_context,
    configure_logging,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

# ---------- redact processor (isolation) ----------------------------------


def test_redact_masks_default_sensitive_keys() -> None:
    redact = _make_redact_processor(DEFAULT_SENSITIVE_KEYS)
    event = {"event": "test", "api_key": "secret", "password": "hunter2"}
    result = redact(None, "info", event)
    assert result["api_key"] == "***"
    assert result["password"] == "***"
    assert result["event"] == "test"


def test_redact_preserves_non_sensitive_keys() -> None:
    redact = _make_redact_processor(DEFAULT_SENSITIVE_KEYS)
    event = {"event": "test", "user_id": 42, "request_path": "/api/v1/foo"}
    result = redact(None, "info", event)
    assert result["user_id"] == 42
    assert result["request_path"] == "/api/v1/foo"


def test_redact_is_case_insensitive() -> None:
    redact = _make_redact_processor(DEFAULT_SENSITIVE_KEYS)
    event = {"API_KEY": "x", "Authorization": "Bearer y", "Cookie": "session=z"}
    result = redact(None, "info", event)
    assert result["API_KEY"] == "***"
    assert result["Authorization"] == "***"
    assert result["Cookie"] == "***"


def test_redact_handles_empty_event_dict() -> None:
    redact = _make_redact_processor(DEFAULT_SENSITIVE_KEYS)
    assert redact(None, "info", {}) == {}


def test_redact_with_extra_keys_masks_them() -> None:
    extended = DEFAULT_SENSITIVE_KEYS | frozenset({"databricks_token"})
    redact = _make_redact_processor(extended)
    event = {"event": "test", "databricks_token": "dapi123", "user": "alice"}
    result = redact(None, "info", event)
    assert result["databricks_token"] == "***"
    assert result["user"] == "alice"


# ---------- trace context processor (isolation) ---------------------------


def test_add_trace_context_no_op_when_no_active_span() -> None:
    event = {"event": "test"}
    result = add_trace_context(None, "info", event)
    assert "trace_id" not in result
    assert "span_id" not in result
    assert result == {"event": "test"}


def test_add_trace_context_injects_when_span_is_active() -> None:
    # Set up a real provider so start_as_current_span produces a valid context.
    # contextvars-based current-span lookup works regardless of which provider
    # the tracer came from, so we don't need to install this globally.
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("test_span"):
        event: dict = {"event": "inside_span"}
        result = add_trace_context(None, "info", event)

    assert "trace_id" in result
    assert "span_id" in result
    # OTel-standard hex formatting: 128-bit trace_id, 64-bit span_id.
    assert len(result["trace_id"]) == 32
    assert len(result["span_id"]) == 16
    # Both should be lowercase hex
    int(result["trace_id"], 16)  # raises ValueError if not hex
    int(result["span_id"], 16)


def test_add_trace_context_no_op_outside_span_after_one_ran() -> None:
    """Verify the processor doesn't leak span state across calls."""
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("inside"):
        pass  # span ends here

    event: dict = {"event": "outside"}
    result = add_trace_context(None, "info", event)
    assert "trace_id" not in result
    assert "span_id" not in result


# ---------- configure_logging (integration via caplog) --------------------


@pytest.fixture
def configured_logger(caplog: pytest.LogCaptureFixture) -> structlog.stdlib.BoundLogger:
    """configure_logging + a fresh structlog logger; caplog captures output."""
    configure_logging()
    caplog.set_level("INFO")
    return structlog.get_logger("ai_ops_kit.test")


def _last_json(caplog: pytest.LogCaptureFixture) -> dict:
    """Parse the last captured log message as JSON."""
    assert caplog.records, "no log records captured"
    return json.loads(caplog.records[-1].getMessage())


def test_log_output_is_json_with_expected_fields(
    configured_logger: structlog.stdlib.BoundLogger, caplog: pytest.LogCaptureFixture
) -> None:
    configured_logger.info("test_event", custom_field="value")
    record = _last_json(caplog)
    assert record["event"] == "test_event"
    assert record["custom_field"] == "value"
    assert record["level"] == "info"
    assert "timestamp" in record
    # ISO-8601 timestamp ends with 'Z' (UTC) by default
    assert record["timestamp"].endswith("Z")


def test_log_redacts_sensitive_field(
    configured_logger: structlog.stdlib.BoundLogger, caplog: pytest.LogCaptureFixture
) -> None:
    configured_logger.info("auth_attempt", api_key="secret-key-123", user="alice")
    record = _last_json(caplog)
    assert record["api_key"] == "***"
    assert record["user"] == "alice"


def test_configure_logging_extra_sensitive_keys_merge_with_defaults(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging(extra_sensitive_keys={"databricks_token"})
    caplog.set_level("INFO")
    log = structlog.get_logger("ai_ops_kit.test_extra")
    log.info("connect", databricks_token="dapi123", password="x", normal="ok")
    record = _last_json(caplog)
    # Extra key redacted
    assert record["databricks_token"] == "***"
    # Default key still redacted
    assert record["password"] == "***"
    # Non-sensitive key preserved
    assert record["normal"] == "ok"


def test_log_with_active_span_includes_trace_ids(
    configured_logger: structlog.stdlib.BoundLogger, caplog: pytest.LogCaptureFixture
) -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("traced_block"):
        configured_logger.info("inside")

    record = _last_json(caplog)
    assert "trace_id" in record
    assert "span_id" in record
    assert len(record["trace_id"]) == 32
    assert len(record["span_id"]) == 16


def test_log_outside_any_span_omits_trace_ids(
    configured_logger: structlog.stdlib.BoundLogger, caplog: pytest.LogCaptureFixture
) -> None:
    configured_logger.info("bare_log")
    record = _last_json(caplog)
    assert "trace_id" not in record
    assert "span_id" not in record


def test_log_warning_level_routes_through(
    configured_logger: structlog.stdlib.BoundLogger, caplog: pytest.LogCaptureFixture
) -> None:
    configured_logger.warning("watch_this", reason="threshold_exceeded")
    record = _last_json(caplog)
    assert record["level"] == "warning"
    assert record["reason"] == "threshold_exceeded"
