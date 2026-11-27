"""Test-collection setup for the webhook_api test package.

`apps/webhook_api/main.py` parses `WebhookAPISettings()` at import time (it
needs a module-level `app` object for uvicorn to discover as
`apps.webhook_api.main:app`), so any test module that imports it needs
`REVIEW_WEBHOOK_API_ENVIRONMENT`, `REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET`,
`REVIEW_WEBHOOK_API_GITHUB_APP_ID`/`REVIEW_WEBHOOK_API_GITHUB_APP_PRIVATE_KEY`
(the synchronous reaction-fetch path needs its own installation-token
exchange capability), and the bare `REVIEW_DATABASE_URL` set before that import happens —
all required fields with no default (see settings.py). The private-key placeholder below
is a syntactically-arbitrary string, not a real PEM: fine for module import
(`WebhookAPISettings` never parses it eagerly, only `GitHubInstallationTokenProvider`
does, at actual call time), but any test that exercises the real
synchronous-fetch path builds its own `WebhookAPISettings` with a real
generated key (`test_rsa_private_key_pem`-shaped) instead of relying on
this placeholder.
pytest imports `conftest.py` before collecting sibling test modules in this
directory, so setting defaults here (only if not already set) covers every
test file in this package without each one repeating it. Tests that need a
*specific* secret/settings value (e.g. to compute a matching webhook
signature, or a real testcontainers Postgres DSN) construct their own
`WebhookAPISettings(...)` directly instead of relying on these process-wide
defaults. The `REVIEW_DATABASE_URL` default below is a syntactically valid
but unreachable DSN — safe at import time only because
`adapter.storage.engine.create_storage_engine`/`create_session_factory` are
non-connecting, synchronous factory calls (see their own docstrings); any
test that actually exercises a DB-touching route builds its own settings
with a real testcontainers DSN instead.

Per docs/rules/testing.md's middle tier: every DB-touching integration test
in this package runs against a real Postgres via `testcontainers-python` —
`migrated_engine`/`pgvector_container` are imported from
`adapter/storage/conftest.py` (not redefined here) so this package reuses
that same fixture machinery rather than a second, near-duplicate copy.
"""

import os

from adapter.storage.conftest import migrated_engine, pgvector_container  # noqa: F401
from adapter.telemetry.conftest import reset_otel_global_providers  # noqa: F401

os.environ.setdefault("REVIEW_WEBHOOK_API_ENVIRONMENT", "development")
os.environ.setdefault("REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET", "conftest-default-webhook-secret")
os.environ.setdefault("REVIEW_WEBHOOK_API_GITHUB_APP_ID", "1")
os.environ.setdefault(
    "REVIEW_WEBHOOK_API_GITHUB_APP_PRIVATE_KEY", "conftest-default-placeholder-key"
)
os.environ.setdefault(
    "REVIEW_DATABASE_URL", "postgresql+asyncpg://unused:unused@localhost:5432/unused"
)
