"""Test-collection setup for the `review` package.

Per docs/rules/testing.md's middle tier: `migrated_engine`/`pgvector_container`
are imported from `adapter/storage/conftest.py` (not redefined here) so this
package reuses the same fixture machinery `apps/webhook_api`/`apps/worker`
already do, rather than a second, near-duplicate copy — needed by
`review/test_review_job.py` (Phase 7), the first test in this package that
drives a real Postgres-backed job queue end to end.

`docs/plans/plan-deliver-memory.md`'s Phase 6 section (Unit F) adds
`review/test_verification_step.py`'s own Unit E -> Unit F contract test,
which imports `apps.webhook_api.main` to build real `posted_finding_comment`/
`memory_entry` fixture rows via `PostgresMemoryRepository` (Phase 3 of
`docs/plans/plan-deliver-admin-gui.md` moved that contract test's own
admin-approve step to drive `apps.admin_api.main` instead, since the Memory
admin API itself now lives there — see that test module's own docstring).
`apps/webhook_api/main.py` parses `WebhookAPISettings()` at import time (a
module-level `app` object for uvicorn) — the same reason
`apps/webhook_api/conftest.py` sets these defaults for its own package;
duplicated here (only if not already set, same `os.environ.setdefault`
guard) rather than imported across the package boundary, since this
package's own tests that need a *specific* value still construct their own
`WebhookAPISettings(...)` directly, matching `apps/webhook_api/conftest.py`'s
own docstring reasoning exactly. Importing `apps.admin_api.main` carries the
same "parses settings at module-import time" requirement for
`AdminAPISettings()` — the defaults below mirror
`apps/admin_api/conftest.py`'s own, for the same reason.
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
os.environ.setdefault("REVIEW_ADMIN_API_ENVIRONMENT", "development")
os.environ.setdefault("REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_USERNAME", "conftest-default-admin")
os.environ.setdefault(
    "REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_PASSWORD", "conftest-default-bootstrap-password"
)
os.environ.setdefault("REVIEW_ADMIN_API_ADMIN_CSRF_SECRET", "conftest-default-csrf-secret")
