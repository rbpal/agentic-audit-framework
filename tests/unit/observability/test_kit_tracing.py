"""Unit tests for `ai_ops_kit.tracing`.

Covers the three public symbols:

- ``init_tracer``: provider setup, resource attributes, exporter selection
  (OTLP vs console), idempotency.
- ``get_tracer``: returns a tracer.
- ``trace_context``: emits a parent span with attributes; nested spans
  inherit parent.

Uses ``InMemorySpanExporter`` to capture spans without touching real
OTel infrastructure. ``opentelemetry.trace.get_tracer`` is monkeypatched
to route tracer requests to a test provider for span-emission tests;
``opentelemetry.trace.set_tracer_provider`` is monkeypatched to capture
the provider for ``init_tracer`` setup tests.
"""

from __future__ import annotations

import pytest
from ai_ops_kit import tracing as tracing_module
from ai_ops_kit.tracing import get_tracer, init_tracer, trace_context
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# ---------- fixtures ------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_initialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``init_tracer`` to behave as if never called in each test."""
    monkeypatch.setattr(tracing_module, "_INITIALIZED", False)


@pytest.fixture
def in_memory_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Route ``trace.get_tracer`` calls to a test provider with an in-memory exporter.

    Avoids OTel's "global TracerProvider is set once per process" constraint
    by replacing the lookup rather than re-installing the provider.
    """
    exporter = InMemorySpanExporter()
    test_provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    test_provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        "opentelemetry.trace.get_tracer",
        lambda name, *args, **kwargs: test_provider.get_tracer(name),
    )
    return exporter


@pytest.fixture
def captured_providers(monkeypatch: pytest.MonkeyPatch) -> list[TracerProvider]:
    """Capture providers passed to ``trace.set_tracer_provider``."""
    captured: list[TracerProvider] = []
    monkeypatch.setattr(
        "opentelemetry.trace.set_tracer_provider",
        lambda provider: captured.append(provider),
    )
    return captured


# ---------- init_tracer: resource attributes ------------------------------


def test_init_tracer_sets_service_name(captured_providers: list[TracerProvider]) -> None:
    init_tracer("my_service")
    assert captured_providers[0].resource.attributes["service.name"] == "my_service"


def test_init_tracer_defaults_service_version_to_0_0_0(
    captured_providers: list[TracerProvider],
) -> None:
    init_tracer("svc")
    assert captured_providers[0].resource.attributes["service.version"] == "0.0.0"


def test_init_tracer_defaults_deployment_environment_to_development(
    captured_providers: list[TracerProvider],
) -> None:
    init_tracer("svc")
    assert captured_providers[0].resource.attributes["deployment.environment"] == "development"


def test_init_tracer_arg_overrides_env_var(
    captured_providers: list[TracerProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVICE_VERSION", "from-env")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "from-env")
    init_tracer("svc", service_version="from-arg", deployment_environment="from-arg")
    attrs = captured_providers[0].resource.attributes
    assert attrs["service.version"] == "from-arg"
    assert attrs["deployment.environment"] == "from-arg"


def test_init_tracer_env_var_used_when_no_arg(
    captured_providers: list[TracerProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVICE_VERSION", "1.2.3")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "staging")
    init_tracer("svc")
    attrs = captured_providers[0].resource.attributes
    assert attrs["service.version"] == "1.2.3"
    assert attrs["deployment.environment"] == "staging"


# ---------- init_tracer: exporter selection -------------------------------


def _exporter_from(provider: TracerProvider) -> object:
    """Pluck the exporter out of the provider's first BatchSpanProcessor."""
    processor = provider._active_span_processor._span_processors[0]  # type: ignore[attr-defined]
    assert isinstance(processor, BatchSpanProcessor)
    return processor.span_exporter  # type: ignore[attr-defined]


def test_init_tracer_uses_console_exporter_when_no_endpoint(
    captured_providers: list[TracerProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    init_tracer("svc")
    assert isinstance(_exporter_from(captured_providers[0]), ConsoleSpanExporter)


def test_init_tracer_uses_otlp_exporter_when_endpoint_given(
    captured_providers: list[TracerProvider],
) -> None:
    init_tracer("svc", otlp_endpoint="http://collector:4317")
    assert isinstance(_exporter_from(captured_providers[0]), OTLPSpanExporter)


def test_init_tracer_uses_otlp_endpoint_env_var_when_no_arg(
    captured_providers: list[TracerProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://from-env:4317")
    init_tracer("svc")
    assert isinstance(_exporter_from(captured_providers[0]), OTLPSpanExporter)


# ---------- init_tracer: idempotency --------------------------------------


def test_init_tracer_idempotent(captured_providers: list[TracerProvider]) -> None:
    init_tracer("first")
    init_tracer("second")
    assert len(captured_providers) == 1, "set_tracer_provider should be called only once"


def test_init_tracer_returns_tracer(captured_providers: list[TracerProvider]) -> None:
    tracer = init_tracer("svc")
    assert tracer is not None


# ---------- get_tracer ----------------------------------------------------


def test_get_tracer_returns_tracer(in_memory_exporter: InMemorySpanExporter) -> None:
    assert get_tracer("any.module") is not None


# ---------- trace_context: span emission ----------------------------------


def test_trace_context_emits_named_span(in_memory_exporter: InMemorySpanExporter) -> None:
    with trace_context("audit_pipeline_run"):
        pass
    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "audit_pipeline_run"


def test_trace_context_sets_attributes(in_memory_exporter: InMemorySpanExporter) -> None:
    with trace_context("audit_pipeline_run", scenario_id="DC-9.A", run_id="abc123"):
        pass
    spans = in_memory_exporter.get_finished_spans()
    assert spans[0].attributes is not None
    assert spans[0].attributes["scenario_id"] == "DC-9.A"
    assert spans[0].attributes["run_id"] == "abc123"


def test_trace_context_nested_spans_inherit_parent(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    tracer = get_tracer("nested_test")
    with trace_context("parent_span"), tracer.start_as_current_span("child_span"):
        pass
    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 2
    child = next(s for s in spans if s.name == "child_span")
    parent = next(s for s in spans if s.name == "parent_span")
    assert child.parent is not None
    assert child.parent.span_id == parent.context.span_id


def test_trace_context_with_no_attributes_still_emits_span(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    with trace_context("bare_span"):
        pass
    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "bare_span"


def test_trace_context_yields_span_for_attribute_setting_inside(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    with trace_context("with_late_attr") as span:
        span.set_attribute("late_attr", "late_value")
    spans = in_memory_exporter.get_finished_spans()
    assert spans[0].attributes is not None
    assert spans[0].attributes["late_attr"] == "late_value"


# ---------- trace_context: exception propagation --------------------------


def test_trace_context_propagates_exceptions(in_memory_exporter: InMemorySpanExporter) -> None:
    class _DeliberateError(RuntimeError):
        pass

    with pytest.raises(_DeliberateError), trace_context("doomed_span"):
        raise _DeliberateError("by design")

    # Span should still close cleanly despite the exception.
    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "doomed_span"
