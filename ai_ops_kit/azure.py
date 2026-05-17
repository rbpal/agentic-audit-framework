"""Azure Monitor wiring for ai_ops_kit.

One public symbol: ``configure_azure_monitor(connection_string=None)``.

A thin wrapper around Microsoft's ``azure.monitor.opentelemetry.configure_azure_monitor``
so the kit owns the integration seam. Consumers call ``ai_ops_kit.configure_azure_monitor(...)``
rather than depending on Microsoft's API directly — which lets us swap to raw OTLP later
(or to a different Azure SDK release) by editing one function body, without touching
any call sites in Project 1, Project 2, or Project 3.

The wrapper adds three behaviours on top of Microsoft's distro:

1. **Env-var fallback** — if ``connection_string`` is not passed, reads
   ``APPLICATIONINSIGHTS_CONNECTION_STRING`` from the environment. Raises
   ``ValueError`` if neither is set (fail-fast — silent no-op would be worse).
2. **Idempotency** — second and subsequent calls are no-ops. Microsoft's distro
   does not deduplicate by default, and double-init can produce duplicate spans.
3. **Lazy import** — the Microsoft package (``azure.monitor.opentelemetry``)
   has a known version conflict with our pinned ``opentelemetry-sdk==1.41.x``
   as of 2026-05-17 (their exporter expects SDK ``==1.40`` and imports
   ``LogData`` which was removed in 1.41). Importing it at module load would
   break ``from ai_ops_kit import ...`` for everyone, even consumers that
   don't use Azure Monitor. So the import lives inside the function body —
   only callers that actually invoke ``configure_azure_monitor()`` pay the
   cost (and would hit the ImportError until Microsoft ships a 1.41-
   compatible exporter beta, or we pin the full OTel stack to 1.40).

Usage::

    from ai_ops_kit import configure_azure_monitor

    # Explicit connection string (Terraform output, Key Vault, etc.):
    configure_azure_monitor(connection_string="InstrumentationKey=...;IngestionEndpoint=...")

    # Or via env var (preferred — keeps the credential out of code):
    # $ export APPLICATIONINSIGHTS_CONNECTION_STRING="InstrumentationKey=...;..."
    configure_azure_monitor()
"""

from __future__ import annotations

import os

_INITIALIZED = False
_ENV_VAR = "APPLICATIONINSIGHTS_CONNECTION_STRING"


def configure_azure_monitor(connection_string: str | None = None) -> None:
    """Configure OTel span/metric/log export to Azure Application Insights.

    Idempotent — second and subsequent calls return without re-initializing.
    Microsoft's underlying distro does NOT deduplicate, so a second call would
    install a second exporter and produce duplicate spans; the guard here
    matches the pattern in :func:`ai_ops_kit.tracing.init_tracer`.

    Args:
        connection_string: Application Insights connection string. If ``None``,
            falls back to the ``APPLICATIONINSIGHTS_CONNECTION_STRING`` env var.
            The connection string is a credential — pass it via env var or
            Key Vault in production, never check it into source.

    Raises:
        ValueError: if neither ``connection_string`` nor the env var is set.
        ImportError: if ``azure.monitor.opentelemetry`` cannot be imported,
            typically due to the OTel SDK version conflict noted in this
            module's docstring. Mitigation paths documented there.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return

    resolved = connection_string or os.environ.get(_ENV_VAR)
    if not resolved:
        raise ValueError(
            f"configure_azure_monitor requires either the connection_string arg "
            f"or the {_ENV_VAR} env var; neither was provided."
        )

    # Lazy import — see module docstring for the OTel SDK version conflict
    # that motivates deferring this until call time.
    from azure.monitor.opentelemetry import configure_azure_monitor as _azure_configure

    _azure_configure(connection_string=resolved)
    _INITIALIZED = True
