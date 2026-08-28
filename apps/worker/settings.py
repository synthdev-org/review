"""Configuration for the Worker entrypoint, parsed once here via pydantic-settings.

Per docs/rules/architecture.md's Decoupling section: config is parsed once at
each entrypoint's composition root and passed down explicitly — domain and
adapter code never read env vars, Kubernetes Secrets, or files directly.

Phase 3 of the delivery plan extends this from Phase 1's near-empty model
with the LLM Gateway's bootstrap config (endpoint + credential fields), per
the C4 doc's "Configuration & Secrets" section: the LLM endpoint is
bootstrap config, read once here, never sourced from Postgres. The Webhook
API has no equivalent fields — per the C4 doc's Level 3 diagram, the LLM
Gateway is "used by Worker only."
"""

from __future__ import annotations

from typing import Literal

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Bootstrap settings for the Worker process.

    `environment` has no default on purpose: an entrypoint that silently
    defaulted it could boot as "development" in production by accident.
    The LLM fields *do* default — to the local LM Studio instance Phase 3's
    Docker Compose target points at (per the C4 doc's Level 3 diagram) —
    because, unlike `environment`, an LLM misconfiguration is caught by
    this phase's own fail-fast startup connectivity check
    (`llmgateway.startup_check.check_llm_reachable`, wired in
    `apps/worker/main.py`) rather than silently running in a wrong mode; a
    production deployment overrides these via Helm `Secret`/`existingSecret`
    values same as any other bootstrap config.

    `llm_api_key` is `pydantic.SecretStr` per the plan's Security review
    ("GitHub App private key, webhook secret, and any LLM API key ... must
    never appear in logs") — never logged or `repr()`-able by accident.
    """

    model_config = SettingsConfigDict(
        env_prefix="REVIEW_WORKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"]
    log_level: str = "INFO"

    llm_endpoint: AnyHttpUrl = AnyHttpUrl("http://host.docker.internal:1234/v1")
    llm_api_key: SecretStr = SecretStr("not-needed")
    llm_model: str = "local-model"
    llm_timeout_seconds: float = 30.0
