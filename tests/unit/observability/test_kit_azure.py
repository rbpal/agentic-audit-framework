"""Unit tests for `ai_ops_kit.azure.configure_azure_monitor`.

Covers the wrapper's behaviour without invoking Microsoft's actual distro
(which has a known OTel SDK version conflict as of 2026-05-17 — see
``ai_ops_kit/azure.py`` module docstring). The fake distro is injected
into ``sys.modules`` so the lazy import inside the wrapper resolves to
the mock instead of triggering the broken import chain.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest
from ai_ops_kit import azure as azure_module
from ai_ops_kit.azure import configure_azure_monitor

# ---------- fixtures ------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_initialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``configure_azure_monitor`` to behave as if never called per test."""
    monkeypatch.setattr(azure_module, "_INITIALIZED", False)


@pytest.fixture
def mock_distro(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Inject a fake `azure.monitor.opentelemetry` so the lazy import succeeds.

    The wrapper's lazy import is::

        from azure.monitor.opentelemetry import configure_azure_monitor as _azure_configure

    Putting a fake module in ``sys.modules`` makes the import resolve to the
    fake; we hand back the fake's ``configure_azure_monitor`` attribute so
    tests can assert on its call args.
    """
    fake_configure = MagicMock(name="azure_distro.configure_azure_monitor")
    fake_module = MagicMock()
    fake_module.configure_azure_monitor = fake_configure
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", fake_module)
    return fake_configure


@pytest.fixture
def _no_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the env var is unset so explicit-arg tests test what they say."""
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)


# ---------- credential resolution -----------------------------------------


def test_raises_when_no_connection_string_and_no_env_var(_no_env_var: None) -> None:
    with pytest.raises(ValueError, match="connection_string"):
        configure_azure_monitor()


def test_uses_explicit_connection_string_arg(_no_env_var: None, mock_distro: MagicMock) -> None:
    configure_azure_monitor(connection_string="InstrumentationKey=arg-key;Ingestion=arg")
    mock_distro.assert_called_once_with(
        connection_string="InstrumentationKey=arg-key;Ingestion=arg"
    )


def test_falls_back_to_env_var_when_no_arg(
    monkeypatch: pytest.MonkeyPatch, mock_distro: MagicMock
) -> None:
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=env-key;")
    configure_azure_monitor()
    mock_distro.assert_called_once_with(connection_string="InstrumentationKey=env-key;")


def test_explicit_arg_takes_precedence_over_env_var(
    monkeypatch: pytest.MonkeyPatch, mock_distro: MagicMock
) -> None:
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=from-env;")
    configure_azure_monitor(connection_string="InstrumentationKey=from-arg;")
    mock_distro.assert_called_once_with(connection_string="InstrumentationKey=from-arg;")


# ---------- idempotency ---------------------------------------------------


def test_second_call_is_no_op(_no_env_var: None, mock_distro: MagicMock) -> None:
    configure_azure_monitor(connection_string="InstrumentationKey=first;")
    configure_azure_monitor(connection_string="InstrumentationKey=second;")
    # Despite two calls, the underlying distro is invoked only once.
    assert mock_distro.call_count == 1
    mock_distro.assert_called_once_with(connection_string="InstrumentationKey=first;")


def test_second_call_does_not_raise_even_without_creds(mock_distro: MagicMock) -> None:
    """After successful init, missing creds on subsequent calls must not raise.

    The idempotency guard runs before the credential check, so once
    initialized the function returns immediately.
    """
    configure_azure_monitor(connection_string="InstrumentationKey=init;")
    # Now env var is whatever it is; explicit arg None. Should not raise.
    configure_azure_monitor()  # second call, idempotent


# ---------- import-time safety --------------------------------------------


def test_importing_ai_ops_kit_does_not_eagerly_import_microsoft_distro() -> None:
    """The kit must load even when azure.monitor.opentelemetry is broken.

    Microsoft's distro has an OTel SDK version conflict (as of 2026-05-17,
    their exporter imports ``LogData`` which was removed in OTel SDK 1.41).
    The kit's ``__init__.py`` should not trigger this import. If a future
    change converts the lazy import to eager, this test catches it: imports
    of ai_ops_kit would fail BEFORE this test runs, breaking the whole suite.
    Surviving collection + the assertions below confirms the lazy pattern
    holds.
    """
    # If we got here, ai_ops_kit imported successfully — no Azure module load.
    from ai_ops_kit import (
        configure_azure_monitor,
        configure_logging,
        get_tracer,
        init_tracer,
        trace_context,
        traced_agent,
        traced_llm_call,
        traced_tool,
    )

    assert configure_azure_monitor is not None
    # Confirm the symbol is from our module, not Microsoft's (defensive).
    assert configure_azure_monitor.__module__ == "ai_ops_kit.azure"
    # Confirm full kit surface still importable alongside.
    for sym in (
        configure_logging,
        get_tracer,
        init_tracer,
        trace_context,
        traced_agent,
        traced_llm_call,
        traced_tool,
    ):
        assert sym is not None
