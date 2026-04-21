"""Smoke test for agentic_audit.config.Settings.

Three assertions:
  1. Package exposes __version__.
  2. Settings loads successfully when required env vars are provided.
  3. Settings rejects non-HTTPS endpoints (security invariant).
"""

import pytest

from agentic_audit import __version__
from agentic_audit.config import Settings


def test_version_exists() -> None:
    """Package should expose semver version string."""
    assert __version__ == "0.1.0"


def test_settings_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings populates from env vars and exposes typed fields."""
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fake.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "fake-key-123")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "fake-deployment")

    s = Settings()  # type: ignore[call-arg]

    assert str(s.azure_openai_endpoint).startswith("https://")
    assert s.azure_openai_api_key.get_secret_value() == "fake-key-123"
    assert s.azure_openai_deployment_name == "fake-deployment"


def test_settings_rejects_http_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings must refuse HTTP (non-TLS) endpoints for security."""
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "http://insecure.example.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "fake-deployment")

    with pytest.raises(ValueError, match="must start with https"):
        Settings()  # type: ignore[call-arg]
