"""Application settings, loaded from environment variables (prefix ``AITSEV_``)."""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class SandboxMode(StrEnum):
    """Where verification commands run."""

    DOCKER = "docker"  # isolated container, no network (production default)
    LOCAL = "local"  # plain subprocess in a temp dir (development and tests only)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AITSEV_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- service ---
    environment: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./aitsev.db"
    auto_create_schema: bool = Field(
        default=False,
        description="Create tables on startup. Use Alembic migrations in production.",
    )
    # "name:key,name2:key2". Each name owns its own tasks.
    api_keys: SecretStr = SecretStr("")
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- model providers ---
    ollama_cloud_base_url: str = "https://ollama.com"
    ollama_api_key: SecretStr | None = Field(default=None, validation_alias="OLLAMA_API_KEY")
    ollama_local_base_url: str | None = None  # e.g. http://localhost:11434
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    model_timeout_seconds: float = 300.0
    model_max_retries: int = 3
    model_retry_base_delay: float = 1.0

    # --- model roles (provider/model). Confirm names with GET /v1/models. ---
    planner_model: str = "ollama-cloud/gpt-oss:120b"
    generator_models: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "ollama-cloud/kimi-k2.6",
            "ollama-cloud/deepseek-v4-flash",
            "ollama-cloud/glm-5",
        ]
    )
    reviewer_models: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["ollama-cloud/gpt-oss:120b", "ollama-cloud/glm-5"]
    )
    judge_model: str = "ollama-cloud/gpt-oss:120b"
    fallback_models: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["ollama-cloud/gpt-oss:120b"]
    )

    # --- pipeline ---
    max_repair_attempts: int = 2
    generation_max_tokens: int = 32_000
    structured_max_tokens: int = 4_096
    # Whole-project upload limits (all files are written to the sandbox for verification).
    max_task_files: int = 2_000
    max_task_bytes: int = 5_000_000
    max_file_bytes: int = 500_000
    # What the models read: the context_paths subset (or all files when it is not given).
    max_context_bytes: int = 400_000
    max_verify_commands: int = 10
    max_output_files: int = 100
    worker_concurrency: int = 2
    worker_poll_seconds: float = 2.0

    # --- accounts and public release (phase 5) ---
    public_url: str = "http://localhost:8000"  # used in email links
    allow_signup: bool = True
    # None = required outside development
    require_email_verification: bool | None = None
    session_days: int = 30
    # None = Secure cookies outside development
    cookie_secure: bool | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    email_from: str = "Aitsev <no-reply@aitsev.local>"
    web_dist_dir: str | None = None  # built web app, served at /app
    default_plan: str = "free"
    # Where /download/<platform> redirects, e.g.
    # https://github.com/<owner>/<repo>/releases/latest/download
    download_base_url: str | None = None
    # JSON list overriding the models users can choose (see aitsev/catalog.py).
    model_catalog: str | None = None

    # --- integrations (phase 3) ---
    # Encrypts stored connection credentials. Any long random string; required outside development.
    # Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"
    secret_key: SecretStr | None = None
    # Block connections to private/loopback addresses (SSRF protection). None = allow only in
    # development.
    allow_private_networks: bool | None = None
    integration_timeout_seconds: float = 60.0
    integration_max_retries: int = 3
    job_max_records: int = 1_000_000
    job_max_error_rate: float = 0.05
    job_sample_size: int = 20

    # --- sandbox ---
    sandbox_mode: SandboxMode = SandboxMode.DOCKER
    sandbox_image: str = "python:3.12-slim"
    sandbox_timeout_seconds: int = 600
    sandbox_memory: str = "2g"
    sandbox_cpus: str = "2"
    sandbox_output_limit: int = 20_000
    # Directory for sandbox workspaces. When the API itself runs in Docker and starts sibling
    # containers through the host's Docker socket, mount this path at the SAME path on the host.
    sandbox_work_root: str | None = None

    @field_validator(
        "ollama_api_key",
        "openai_api_key",
        "anthropic_api_key",
        "secret_key",
        "smtp_password",
        mode="before",
    )
    @classmethod
    def _empty_secret_is_unset(cls, value: object) -> object:
        """An empty line like ``OLLAMA_API_KEY=`` in .env means "not configured"."""
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        return None if isinstance(raw, str) and not raw.strip() else value

    @field_validator(
        "generator_models", "reviewer_models", "fallback_models", "cors_origins", mode="before"
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    def email_verification_required(self) -> bool:
        if self.require_email_verification is not None:
            return self.require_email_verification
        return self.environment != "development"

    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.environment != "development"

    def private_networks_allowed(self) -> bool:
        if self.allow_private_networks is not None:
            return self.allow_private_networks
        return self.environment == "development"

    def parsed_api_keys(self) -> dict[str, str]:
        """Return a mapping of API key -> owner name."""
        result: dict[str, str] = {}
        raw = self.api_keys.get_secret_value()
        for raw_entry in raw.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            name, sep, key = entry.partition(":")
            if not sep or not name.strip() or len(key.strip()) < 16:
                raise ValueError(
                    "AITSEV_API_KEYS entries must look like name:key with keys of 16+ characters"
                )
            result[key.strip()] = name.strip()
        return result


@lru_cache
def get_settings() -> Settings:
    return Settings()
