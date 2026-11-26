"""Alembic migration environment for adapter.storage's models.

Async per docs/rules/architecture.md's "I/O is async" rule — this follows
Alembic's documented "Using Asyncio with Alembic" recipe: the actual
migration run happens inside a sync callback invoked via
``AsyncConnection.run_sync``, driven by ``asyncio.run`` at the top level.

The DSN comes from the ``REVIEW_DATABASE_URL`` environment variable, not
from ``alembic.ini``'s ``sqlalchemy.url`` — Alembic is a standalone CLI
tool invoked by a human or CI, not application code, so reading the
environment here does not violate architecture.md's "never read env vars
from domain or adapter code" rule (that rule targets the running services'
own request/job-handling code, wired up via pydantic-settings instead).
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from adapter.storage.models import Base

# Alembic Config object, providing access to values within alembic.ini.
config = context.config

if config.config_file_name is not None:
    # False: the default (True) silently disables every already-created logger
    # in the host process (tests, worker), dropping their records.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Autogenerate support: Alembic diffs this metadata against the live schema.
target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("REVIEW_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "REVIEW_DATABASE_URL must be set to run Alembic migrations "
            "(e.g. postgresql+asyncpg://user:pass@host:5432/dbname)."
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (`--sql` mode)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations_sync(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live database using an async engine."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations_sync)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
