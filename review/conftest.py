"""Test-collection setup for the `review` package.

Per docs/rules/testing.md's middle tier: `migrated_engine`/`pgvector_container`
are imported from `adapter/storage/conftest.py` (not redefined here) so this
package reuses the same fixture machinery `apps/webhook_api`/`apps/worker`
already do, rather than a second, near-duplicate copy — needed by
`review/test_review_job.py`, the first test in this package that
drives a real Postgres-backed job queue end to end.

`review/test_verification_step.py` has a contract test that imports
`apps.webhook_api.main` to build real `posted_finding_comment`/
`memory_entry` fixture rows via `PostgresMemoryRepository` (its admin-approve
step drives `apps.admin_api.main`, since the Memory admin API lives there;
see that test module's own docstring).
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

import logging
import os

import pytest

from adapter.storage.conftest import migrated_engine, pgvector_container  # noqa: F401
from adapter.telemetry.conftest import reset_otel_global_providers  # noqa: F401
from review.test_finding_judge_support import points  # noqa: F401

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


@pytest.fixture(autouse=True)
def _reenable_verification_logger() -> None:
    """Alembic's `env.py` (run by any migrated-engine test earlier in the same
    process) disables loggers it does not know; the verification tests assert on
    `review.verification` records."""
    logging.getLogger("review.verification").disabled = False
