"""Shared integration-test fixtures for adapter.storage.

Per docs/rules/testing.md's middle tier: every DB-touching integration test
in this package runs against a real Postgres via `testcontainers-python`,
never sqlite-in-memory or a mock — pgvector's presence/index behavior isn't
something a substitute engine can stand in for. This needs Docker socket
access on whatever host/container runs `pytest -m integration`.

`reset_otel_global_providers` (re-exported from `adapter.telemetry.conftest`)
is imported here too, mirroring `apps/webhook_api/conftest.py`'s and
`apps/worker/conftest.py`'s identical re-export — US1's
`adapter/storage/test_queue_telemetry.py` needs it to register an
in-memory `MeterProvider` for `adapter.storage.queue`'s module-level meter
to pick up, per docs/specs/spec-observability.md's METRIC & SPAN CATALOG.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from adapter.storage.engine import create_storage_engine
from adapter.telemetry.conftest import reset_otel_global_providers  # noqa: F401

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_ALEMBIC_INI = os.path.join(_REPO_ROOT, "alembic.ini")


def alembic_config_for(dsn: str) -> Config:
    config = Config(_ALEMBIC_INI)
    # env.py reads REVIEW_DATABASE_URL itself (see its module docstring);
    # setting it here rather than via config.set_main_option keeps exactly
    # one code path for how the DSN reaches a migration run.
    os.environ["REVIEW_DATABASE_URL"] = dsn
    return config


@pytest.fixture(scope="module")
def pgvector_container() -> Iterator[PostgresContainer]:
    """A real Postgres instance with the `pgvector` extension *available*
    (not yet `CREATE EXTENSION`-ed — the baseline migration does that)."""
    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="module")
def plain_postgres_container() -> Iterator[PostgresContainer]:
    """A real Postgres instance *without* pgvector installed at all, for
    the pgvector-presence check's negative test."""
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest_asyncio.fixture
async def migrated_engine(pgvector_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    """Run `alembic upgrade head` against a fresh pgvector-capable Postgres
    and hand back a connected async engine. Each test gets a freshly
    migrated (and then torn back down to `base`) schema so tests never leak
    state into each other."""
    dsn = pgvector_container.get_connection_url()
    config = alembic_config_for(dsn)
    # `command.upgrade` runs Alembic's async env.py via its own internal
    # `asyncio.run(...)` (see migrations/env.py) — that call would raise
    # "asyncio.run() cannot be called from a running event loop" if invoked
    # directly from this async fixture's own event loop, so it runs on a
    # separate thread (with no event loop of its own yet) via `to_thread`.
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_storage_engine(dsn)
    try:
        yield engine
    finally:
        await engine.dispose()
        await asyncio.to_thread(command.downgrade, config, "base")
