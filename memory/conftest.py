"""Test-collection setup for the memory test package.

Per docs/rules/testing.md's middle tier: `migrated_engine`/`pgvector_container`
are imported from `adapter/storage/conftest.py` (not redefined here) so this
package reuses the same fixture machinery `apps/webhook_api`/`apps/worker`
already do, rather than a second, near-duplicate copy — this package's
first Integration-tier test module, `memory/test_reaction_poll.py`, is the
first thing in this package that needs it.
"""

from adapter.storage.conftest import migrated_engine, pgvector_container  # noqa: F401
