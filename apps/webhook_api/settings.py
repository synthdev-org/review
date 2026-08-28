"""Configuration for the Webhook API entrypoint, parsed once here via pydantic-settings.

Per docs/rules/architecture.md's Decoupling section: config is parsed once at
each entrypoint's composition root and passed down explicitly — domain and
adapter code never read env vars, Kubernetes Secrets, or files directly.

Phase 1 kept this model intentionally near-empty, anticipating that "later
phases add GitHub App / webhook-secret credentials here as
``pydantic.SecretStr`` fields." Phase 4 is that phase: it adds the webhook
signature secret (required — the webhook route cannot function without
one), the body-size/rate-limit knobs for that route, and the
``SETUP_MODE``-gated manifest/callback flow's configuration, per the plan's
Phase 4 and Security review sections.
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebhookAPISettings(BaseSettings):
    """Bootstrap settings for the Webhook API process.

    Required fields have no default on purpose: an entrypoint that silently
    defaulted ``environment`` (or, as of this phase, the webhook secret)
    could boot in a dangerously-misconfigured state by accident.
    """

    model_config = SettingsConfigDict(
        env_prefix="REVIEW_WEBHOOK_API_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"]
    log_level: str = "INFO"

    # --- Webhook receiver (apps/webhook_api/webhook_routes.py) ---
    # Required: the webhook route's signature verification (this phase's
    # first trust boundary, per the plan's Security review) cannot run
    # without a configured secret. `SecretStr` per architecture.md's
    # credential-handling rule ensures an accidental `repr()`/log-line
    # inclusion renders as a masked placeholder, never the real value.
    github_webhook_secret: SecretStr
    webhook_max_body_bytes: int = 1_000_000
    webhook_rate_limit_max_requests: int = 30
    webhook_rate_limit_window_seconds: float = 60.0

    # --- App manifest/callback setup flow (apps/webhook_api/manifest_routes.py) ---
    # Off by default, per the plan's Phase 4 section ("both ... routes are
    # ... gated behind an explicit SETUP_MODE config flag, off by default")
    # and Security review ("without this gate, any network caller who can
    # reach webhook_api could initiate and complete the App creation flow
    # themselves").
    setup_mode: bool = False
    setup_state_secret: SecretStr | None = None
    setup_state_ttl_seconds: float = 600.0
    github_api_base_url: str = "https://api.github.com"
    github_api_timeout_seconds: float = 10.0

    @model_validator(mode="after")
    def _require_setup_state_secret_when_setup_mode_enabled(self) -> WebhookAPISettings:
        """``setup_state_secret`` signs the manifest flow's CSRF state
        cookie (see ``adapter/github/manifest.py``); it has no safe default
        because a shared/absent value would defeat that protection, so it's
        only required — not merely recommended — the moment an operator
        turns ``setup_mode`` on.
        """
        if self.setup_mode and self.setup_state_secret is None:
            raise ValueError("setup_state_secret is required when setup_mode is enabled")
        return self
