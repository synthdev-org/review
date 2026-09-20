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
from pydantic_settings import SettingsConfigDict

from adapter.telemetry.sdk import OtelSettingsMixin


class WebhookAPISettings(OtelSettingsMixin):
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

    # --- Memory capture (docs/plans/plan-deliver-memory.md's Phase 3
    # section, Unit C) ---
    # Reply-text redaction before persistence, per that phase's own scope
    # bullet — mirrors `apps/worker/settings.py`'s `WorkerSettings.secret_redaction_enabled`
    # field name/default exactly (the same "reused, not reinvented" wiring
    # choice `apps/webhook_api/main.py`'s new `TextRedactor` dependency
    # makes; this entrypoint's first consumer of that Protocol).
    secret_redaction_enabled: bool = True

    # Per-repo capture ceiling (a real gap Phase 3's own plan review pass
    # found): any GitHub account able to comment/react on a public repo's
    # PR can generate valid, signed webhook deliveries that each attempt to
    # create a `pending` `memory_entry` row — not a privileged permission.
    # Reuses the same `InMemoryRateLimiter` shape
    # `review_enqueue_rate_limit_per_minute` already establishes for a
    # materially identical shape of problem, but over a rolling day rather
    # than a minute. Deliberately placed here (`WebhookAPISettings`), not
    # `WorkerSettings` as `docs/plans/plan-deliver-memory.md`'s own prose
    # literally names it — a documented correction, not an oversight: the
    # capture write this ceiling guards
    # (`apps/webhook_api/webhook_routes.py`) runs entirely inside the
    # Webhook API entrypoint, never the Worker, and `WorkerSettings` has no
    # way to reach a config value into a different process's own
    # composition root. No `0`-disables-it sentinel, unlike the enqueue
    # rate limits above — capture has no "off" state of its own per US-M12
    # (only suppression, a later phase's own kill switch, is independently
    # disable-able); a deployment that genuinely wants no ceiling can set
    # this to a very large number instead.
    memory_capture_ceiling_per_repo_per_day: int = 500

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

    # --- Observability (docs/specs/spec-observability.md's Unit A) ---
    # `otel_exporter_otlp_endpoint`/`otel_exporter_otlp_headers` come from
    # `OtelSettingsMixin` above — shared with `WorkerSettings` so the two
    # entrypoints can't silently diverge on this pair's declaration.

    # --- GitHub App auth / synchronous reaction fetch (Phase 4 of
    # plan-deliver-memory.md, Unit D, US-M3's second `Given/When/Then`
    # branch) ---
    # US-M3 requires the reaction fetch for a `pull_request closed` event
    # to run "before the webhook handler returns" — not merely enqueued for
    # the Worker to pick up later (unlike the "later push" branch, which
    # stays an async `MEMORY_REACTION_POLL` job). That means this
    # entrypoint, not just the Worker, needs its own installation-token
    # exchange capability. Required, no default, same fail-fast posture as
    # `apps/worker/settings.py`'s own `github_app_id`/`github_app_private_key`
    # fields (JWT signing cannot function without them) — reusing
    # `github_api_base_url`/`github_api_timeout_seconds` above rather than a
    # second pair of fields for the same GitHub REST API.
    github_app_id: int
    github_app_private_key: SecretStr
    # Same "retries beyond the first attempt" lever
    # `apps/worker/settings.py`'s own field of the same name already
    # documents.
    github_retry_max_attempts: int = 3
    # Same ceiling/reasoning as `apps/worker/settings.py`'s own field of the
    # same name (Assumption/"bounded cost, not just bounded per-job" design)
    # — this entrypoint's own copy, since the synchronous `closed`-trigger
    # poll runs here, not in the Worker.
    memory_reaction_poll_max_comments_per_poll: int = 50

    # --- Admin API (docs/plans/plan-deliver-memory.md's Phase 5 section,
    # Unit E, US-M4/US-M5/US-M8/US-M10) ---
    # Required, no default (Assumption 7) — mirrors `github_webhook_secret`'s
    # own "no safe default, dangerous to silently default" pattern, not
    # `setup_state_secret`'s conditionally-required one: the admin router
    # registers unconditionally (see `apps/webhook_api/main.py`), enforcing
    # auth per-request via a Bearer-token dependency, rather than being
    # registration-gated the way `SETUP_MODE` gates the manifest flow —
    # there is no "off by default" state for the admin API the way there is
    # for the one-time setup flow.
    admin_api_key: SecretStr

    # This entrypoint's own `LocalONNXEmbeddingClient` instance (the admin
    # API's approve route, US-M4, is its first consumer outside the
    # Worker's own job pipeline) — same on-disk, Dockerfile-baked model
    # path/default as `apps/worker/settings.py`'s `embedding_model_path`
    # field, per that file's own docstring for why this is a filesystem
    # path, never a network location or a per-entrypoint override.
    embedding_model_path: str = "/app/models/all-MiniLM-L6-v2"

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
