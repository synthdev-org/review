"""Configuration for the Webhook API entrypoint, parsed once here via pydantic-settings.

Per docs/rules/architecture.md's Decoupling section: config is parsed once at
each entrypoint's composition root and passed down explicitly — domain and
adapter code never read env vars, Kubernetes Secrets, or files directly.

Phase 0 kept this model intentionally near-empty, anticipating that "later
phases add GitHub App / webhook-secret credentials here as
``pydantic.SecretStr`` fields." Phase 4 added the webhook signature secret
(required — the webhook route cannot function without one), the
body-size/rate-limit knobs for that route, and the ``SETUP_MODE``-gated
manifest/callback flow's configuration, per the plan's Phase 4 and Security
review sections. Phase 1 (this phase) adds ``database_url`` (installation/
org/repo resolution and delivery-idempotency need a database) and the
per-repo review-enqueue rate limit, per that phase's own section.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, model_validator
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

    # --- Storage (Phase 1, adapter/storage) ---
    # Sourced from the bare REVIEW_DATABASE_URL env var, not this class's
    # own REVIEW_WEBHOOK_API_ prefix — the same variable name Phase 0's
    # `migrate` Compose service and both Helm Deployments already inject.
    # Required: installation/org/repo resolution and delivery-idempotency
    # (tenancy/resolution.py) cannot function without a database.
    database_url: SecretStr = Field(validation_alias="REVIEW_DATABASE_URL")

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

    # Per-repo review-enqueue rate limit (Phase 1), keyed by repo_id rather
    # than client IP — mitigates unbounded LLM-cost/queue-starvation risk
    # from repeated `synchronize` pushes on one PR. Reverses a stated NFR
    # decision in phase-1-review-pipeline.md ("no additional cross-review
    # rate-limiting is specified for Phase 1"), so it ships **disabled by
    # default** (`0`) per this plan's pre-activation-gate policy for
    # genuine product/cost-policy tradeoffs — Phase 1's own shipped
    # configuration matches that PRD text exactly until a PRD owner
    # explicitly opts in by setting this above `0`. `0` is a documented
    # sentinel handled at the call site (apps/webhook_api/main.py): no
    # `InMemoryRateLimiter` instance is constructed and the enqueue path
    # skips the rate-check call entirely, rather than constructing a
    # limiter and relying on its own `max_requests >= 1` `ValueError` guard.
    review_enqueue_rate_limit_per_minute: int = 0

    # Indexing kill switch (indexing-delivery plan's Phase 1, Assumption
    # 11), defaulting to ``False`` — Phases 1-6 of that plan ship genuinely
    # inert (no `INDEX` job ever enqueued) until an operator makes the
    # affirmative choice to set this `True`, once the Worker actually knows
    # how to run an `INDEX` job (that plan's Phase 7). Scope stated
    # precisely in `apps/webhook_api/webhook_routes.py`: this stops new
    # `INDEX` jobs from being *enqueued*; it does not drain jobs already
    # sitting in `job_queue`.
    indexing_enabled: bool = False

    # Per-repo index-enqueue rate limit (indexing-delivery plan's Phase 1,
    # Assumption 10), keyed by repo id, covering both `push`- and
    # `pull_request`-triggered `INDEX` enqueues together — one bucket per
    # repo, the same shape `review_enqueue_rate_limit_per_minute` above
    # already uses for `REVIEW`. A materially higher recommended default
    # (`5`, not `0`) than `REVIEW`'s: an `INDEX` job's worst-case cost (a
    # full-tree-fallback run, up to 20,000 files/2GB) is high enough that
    # shipping it unthrottled by default is not the same safe-by-default
    # tradeoff `REVIEW`'s `0` default makes. `0` remains a documented
    # sentinel (handled at the call site, `apps/webhook_api/main.py`) for an
    # operator who wants to disable this limit entirely, the same as
    # `review_enqueue_rate_limit_per_minute`'s own `0` sentinel.
    index_enqueue_rate_limit_per_minute: int = 5

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
