"""Test-collection setup for the admin_api test package.

`apps/admin_api/main.py` parses `AdminAPISettings()` at import time (it
needs a module-level `app` object for uvicorn to discover as
`apps.admin_api.main:app`), so any test module that imports it needs
`REVIEW_ADMIN_API_ENVIRONMENT`, `REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_USERNAME`/
`REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_PASSWORD` (Assumption 11 — required, no
default), `REVIEW_ADMIN_API_ADMIN_CSRF_SECRET` (Assumption 13 — required,
no default), and the bare `REVIEW_DATABASE_URL` set before that import
happens — all required fields with no default (see settings.py). The
`REVIEW_DATABASE_URL` default below is a syntactically valid but
unreachable DSN — safe at import time only because
`adapter.storage.engine.create_storage_engine`/`create_session_factory` are
non-connecting, synchronous factory calls; any test that actually
exercises a DB-touching route builds its own settings with a real
testcontainers DSN instead, the same convention
`apps/webhook_api/conftest.py` already establishes.

Per docs/rules/testing.md's middle tier: every DB-touching integration test
in this package runs against a real Postgres via `testcontainers-python` —
`migrated_engine`/`pgvector_container` are imported from
`adapter/storage/conftest.py` (not redefined here) so this package reuses
that same fixture machinery rather than a second, near-duplicate copy.
"""

import os

from adapter.storage.conftest import migrated_engine, pgvector_container  # noqa: F401
from adapter.telemetry.conftest import reset_otel_global_providers  # noqa: F401

os.environ.setdefault("REVIEW_ADMIN_API_ENVIRONMENT", "development")
os.environ.setdefault("REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_USERNAME", "conftest-default-admin")
os.environ.setdefault(
    "REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_PASSWORD", "conftest-default-bootstrap-password"
)
os.environ.setdefault("REVIEW_ADMIN_API_ADMIN_CSRF_SECRET", "conftest-default-csrf-secret")
os.environ.setdefault(
    "REVIEW_DATABASE_URL", "postgresql+asyncpg://unused:unused@localhost:5432/unused"
)
