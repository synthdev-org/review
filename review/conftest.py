"""Test-collection setup for the `review` package.

Per docs/rules/testing.md's middle tier: `migrated_engine`/`pgvector_container`
are imported from `adapter/storage/conftest.py` (not redefined here) so this
package reuses the same fixture machinery `apps/webhook_api`/`apps/worker`
already do, rather than a second, near-duplicate copy — needed by
`review/test_review_job.py` (Phase 7), the first test in this package that
drives a real Postgres-backed job queue end to end.
"""

from adapter.storage.conftest import migrated_engine, pgvector_container  # noqa: F401
