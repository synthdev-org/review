"""Configuration for the Admin API entrypoint, parsed once here via
pydantic-settings, per ``docs/plans/plan-deliver-admin-gui.md``'s Phase 1
section (Unit B) and ``docs/rules/architecture.md``'s Decoupling section:
config is parsed once at each entrypoint's composition root and passed
down explicitly — domain and adapter code never read env vars,
Kubernetes Secrets, or files directly.

Every bootstrap/security-sensitive field with no safe default (
``admin_bootstrap_username``/``admin_bootstrap_password``/
``admin_csrf_secret``) is required, no default — mirrors
``WebhookAPISettings.admin_api_key``'s/``github_webhook_secret``'s own
"no safe default, dangerous to silently default" pattern (Assumption 11).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from adapter.telemetry.sdk import OtelSettingsMixin


class AdminAPISettings(OtelSettingsMixin):
    """Bootstrap settings for the Admin API process."""

    model_config = SettingsConfigDict(
        env_prefix="REVIEW_ADMIN_API_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"]
    log_level: str = "INFO"

    # --- Storage (shared with the other two entrypoints) ---
    # Sourced from the bare REVIEW_DATABASE_URL env var, not this class's
    # own REVIEW_ADMIN_API_ prefix — the same variable name
    # WebhookAPISettings.database_url/WorkerSettings.database_url already
    # reuse, and that the `migrate` Compose service/both Helm Deployments
    # already inject.
    database_url: SecretStr = Field(validation_alias="REVIEW_DATABASE_URL")

    # --- Session cookie (Assumption 7) ---
    # `False` only ever makes sense for a plain-HTTP local dev loopback
    # (the shipped default assumes a TLS-terminating proxy/Ingress in front
    # of every other deployment topology) — mirrors
    # `apps/webhook_api/manifest_routes.py`'s own
    # `secure=request.url.scheme == "https"` judgment call, but as an
    # explicit operator-set flag here (this session cookie, unlike that
    # one-time setup-flow cookie, is this entrypoint's actual standing auth
    # credential) rather than inferred per-request from the incoming
    # request's own scheme, which a TLS-terminating proxy would make appear
    # `http` at this process even when the real client connection is
    # `https`.
    session_cookie_secure: bool = True
    # US-A2's sliding-window idle-expiry window (iam.auth.is_session_expired).
    session_idle_timeout_minutes: int = 30

    # --- Bootstrap admin (Assumption 11) ---
    # Required, no default — every environment, including post-bootstrap
    # restarts, keeps this set; apps/admin_api/bootstrap.py's routine is a
    # no-op past the first run regardless of whether the value is present.
    admin_bootstrap_username: str
    admin_bootstrap_password: SecretStr

    # --- Login rate limiting (Assumption 5) ---
    login_rate_limit_attempts: int = 5
    login_rate_limit_window_minutes: int = 15

    # --- Password policy / Argon2 cost (Assumption 14) ---
    # Enforced server-side before hashing (apps/admin_api/auth_routes.py's
    # bootstrap/Phase 2's user-creation path) — Argon2PasswordHasher's own
    # cost parameters are stated explicitly in
    # adapter/auth/argon2_hasher.py, not configurable here, since nothing
    # in this plan calls for a per-deployment cost override.
    min_password_length: int = 12

    # --- CSRF (Assumption 13) ---
    # Required, no default, mirroring admin_bootstrap_password's own
    # "no safe default" pattern — the key Assumption 13's session-bound
    # CSRF HMAC is keyed by. A new, dedicated secret: the session token
    # itself is an opaque hash-lookup value with no signing key of its own
    # (Assumption 7), so this is never a reuse of any session-cookie
    # material.
    admin_csrf_secret: SecretStr

    # --- Model routing (docs/plans/plan-deliver-admin-gui.md's Phase 5
    # section, Unit F, US-A14/US-A15, Assumption 3) ---
    # The static-default tier `llmgateway.routing.FallbackModelRoutingResolver`
    # falls open to when no `ModelRoutingConfig` row exists for a stage
    # (US-A14's third source). Same literal default as
    # `apps/worker/settings.py`'s own `llm_model` field — a separate,
    # per-entrypoint field (not a shared one) following the exact same
    # "duplicated literal default, not a shared import" precedent this
    # entrypoint's own `embedding_model_path` field already establishes
    # above; an operator running both entrypoints keeps the two env vars in
    # sync the same way they already do for that field.
    llm_model: str = "local-model"

    # --- Memory admin API (docs/plans/plan-deliver-admin-gui.md's Phase 3
    # section, Unit D, US-A7/US-A8-A11) ---
    # This entrypoint's own `LocalONNXEmbeddingClient` instance — the
    # approve route (US-A9/US-M4) moved here from `apps/webhook_api` along
    # with the rest of `apps/webhook_api/admin_routes.py`'s handlers. Same
    # on-disk, Dockerfile-baked model path/default `apps/webhook_api`'s own
    # (now-deleted) field and `apps/worker/settings.py`'s
    # `embedding_model_path` field already use — never a network location
    # or a per-entrypoint override.
    embedding_model_path: str = "/app/models/all-MiniLM-L6-v2"

    # --- Admin-stats dashboard (docs/plans/plan-deliver-admin-gui.md's
    # Phase 6 section, Unit G, US-A17, Assumption 4) ---
    # Points at the Compose `prometheus` service (docker-compose.yml's
    # `observability` profile) by default — a safe, non-secret default (the
    # worst case of it being wrong is a degraded dashboard endpoint, never
    # a security or pipeline-correctness concern, per this phase's own
    # "no kill switch needed" Observability & rollback section), unlike
    # `admin_bootstrap_password`/`admin_csrf_secret` above, which have no
    # default at all. `prometheus_timeout_seconds`/`prometheus_retry_max_attempts`
    # mirror `WorkerSettings.github_api_timeout_seconds`/
    # `github_retry_max_attempts`'s own naming for the same
    # `adapter/github/http.py`-backed retry/backoff shape (Assumption 4) —
    # deliberately lower than that GitHub-calling precedent's own 10s/3
    # defaults: this endpoint serves one read-only dashboard, not a
    # job-dispatch path, so failing fast (a few seconds, not tens) keeps
    # `GET /api/v1/stats` from visibly hanging when Prometheus is slow.
    prometheus_base_url: str = "http://prometheus:9090"
    prometheus_timeout_seconds: float = 5.0
    prometheus_retry_max_attempts: int = 2
