"""OpenTelemetry tracer setup for ai_ops_kit.

Three public symbols:

- ``init_tracer``: one-time global TracerProvider setup. Call at app start.
- ``get_tracer``: per-module tracer accessor. Cheap; call anywhere.
- ``trace_context``: context manager that opens a parent span around a code
  block, so nested spans land as children. Used at the top of a pipeline run.

Design notes:

- ``init_tracer`` is idempotent. Second and subsequent calls return the
  existing tracer without re-installing a provider. OpenTelemetry's global
  ``TracerProvider`` can only be set once per process, so calling twice
  would otherwise emit a warning and silently no-op.
- Endpoint resolution prefers the ``otlp_endpoint`` argument, then falls back
  to the OpenTelemetry-standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var. If
  neither is set, spans go to ``ConsoleSpanExporter`` (development default).
- ``service.version`` and ``deployment.environment`` use the same arg-then-
  env-var-then-default ladder. Defaults are intentionally bland
  ("0.0.0" and "development") so production callers are forced to set them
  explicitly.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
)

_INITIALIZED = False


def init_tracer(
    service_name: str,
    otlp_endpoint: str | None = None,
    service_version: str | None = None,
    deployment_environment: str | None = None,
) -> trace.Tracer:
    """Configure the global OpenTelemetry TracerProvider.

    Idempotent — second and subsequent calls return a tracer for the same
    service without re-installing the provider.

    Args:
        service_name: The ``service.name`` resource attribute. Required.
        otlp_endpoint: gRPC endpoint for ``OTLPSpanExporter``. If ``None``,
            falls back to ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var, then
            to ``ConsoleSpanExporter`` if still unset (dev default).
        service_version: ``service.version`` resource attribute. Falls back
            to ``SERVICE_VERSION`` env var, then ``"0.0.0"``.
        deployment_environment: ``deployment.environment`` resource
            attribute. Falls back to ``DEPLOYMENT_ENVIRONMENT`` env var,
            then ``"development"``.

    Returns:
        A tracer for ``service_name``.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return trace.get_tracer(service_name)

    endpoint = otlp_endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    version = service_version or os.environ.get("SERVICE_VERSION", "0.0.0")
    env = deployment_environment or os.environ.get("DEPLOYMENT_ENVIRONMENT", "development")

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": version,
            "deployment.environment": env,
        }
    )

    provider = TracerProvider(resource=resource)
    exporter: SpanExporter = (
        OTLPSpanExporter(endpoint=endpoint) if endpoint else ConsoleSpanExporter()
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    _INITIALIZED = True
    return trace.get_tracer(service_name)


def get_tracer(module_name: str) -> trace.Tracer:
    """Get a tracer scoped to a module name. Cheap; safe to call anywhere."""
    return trace.get_tracer(module_name)


@contextmanager
def trace_context(span_name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Open a parent span around a code block.

    Used at the top of a pipeline run so all nested spans become children.

    Example::

        with trace_context("audit_pipeline_run", scenario_id="x", run_id="y"):
            # spans created inside are children of this one
            ...

    Args:
        span_name: Name of the parent span.
        **attributes: Key-value attributes set on the parent span.

    Yields:
        The active span.
    """
    tracer = trace.get_tracer("ai_ops_kit.trace_context")
    with tracer.start_as_current_span(span_name) as span:
        for key, value in attributes.items():
            span.set_attribute(key, value)
        yield span
