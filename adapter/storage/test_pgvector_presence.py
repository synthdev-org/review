"""Integration test: the pgvector-presence startup check raises a specific,
typed exception (not a raw driver error) when the extension is missing, and
passes cleanly once the baseline migration has enabled it. Per the plan's
Phase 2 Tests/Acceptance sections, both cases run against a real Postgres
(testcontainers), not a mock.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from adapter.storage.engine import create_storage_engine
from adapter.storage.exceptions import PgVectorNotInstalledError, StorageError
from adapter.storage.pgvector_check import check_pgvector_installed

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_check_passes_once_extension_is_enabled(migrated_engine: AsyncEngine) -> None:
    """The baseline migration runs `CREATE EXTENSION vector`, so a freshly
    migrated database passes the check without raising."""
    await check_pgvector_installed(migrated_engine)


@pytest.mark.asyncio
async def test_check_raises_typed_error_when_extension_missing(
    plain_postgres_container: PostgresContainer,
) -> None:
    """A fresh Postgres instance with no migrations run and no pgvector
    extension installed fails the check with a specific, typed error — not
    a raw asyncpg/SQLAlchemy driver exception."""
    engine = create_storage_engine(plain_postgres_container.get_connection_url())
    try:
        with pytest.raises(PgVectorNotInstalledError) as excinfo:
            await check_pgvector_installed(engine)

        assert isinstance(excinfo.value, StorageError)
        # Credentials/DSN never leak into the error message (see
        # docs/rules/architecture.md's SecretStr/logging rule).
        assert "postgres:postgres" not in str(excinfo.value)
        assert "@" not in str(excinfo.value)
    finally:
        await engine.dispose()
