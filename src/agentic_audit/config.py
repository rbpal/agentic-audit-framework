"""Typed application config loaded from environment.

Single source of truth for every env var the pipeline reads. Import
``Settings`` (or a singleton via ``get_settings()``) instead of calling
``os.getenv`` directly anywhere in the codebase.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime config populated from OS env + .env file.

    Precedence (highest wins): OS env vars > .env file > defaults here.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Azure OpenAI (required for any LLM call) ────────────────────
    azure_openai_endpoint: HttpUrl
    azure_openai_api_key: SecretStr
    azure_openai_deployment_name: str
    azure_openai_api_version: str = "2024-10-21"

    # ── Databricks (needed at Step 2; optional until then) ──────────
    databricks_host: HttpUrl | None = None
    databricks_token: SecretStr | None = None

    # ── MLflow tracking (Step 11) ───────────────────────────────────
    mlflow_tracking_uri: str | None = None

    # ── OpenTelemetry (Step 10) ─────────────────────────────────────
    otel_exporter_otlp_endpoint: str | None = None

    # ── Azure identity (Step 9 Terraform) ───────────────────────────
    azure_subscription_id: str | None = None
    azure_tenant_id: str | None = None

    # ── App runtime config ──────────────────────────────────────────
    log_level: str = "INFO"

    @field_validator("azure_openai_endpoint")
    @classmethod
    def _must_be_https(cls, v: HttpUrl) -> HttpUrl:
        if not str(v).startswith("https://"):
            raise ValueError("AZURE_OPENAI_ENDPOINT must start with https://")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance.

    Callers should prefer this over constructing Settings() directly so
    env parsing happens once per process. Tests that need fresh env
    parsing can instantiate Settings() directly after monkey-patching.
    """
    # pydantic-settings loads required fields from env vars at runtime;
    # mypy can't see that, so call-arg is the correct ignore here.
    return Settings()  # type: ignore[call-arg]
