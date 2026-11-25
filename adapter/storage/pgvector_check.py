"""adapter.storage.pgvector_check — startup check that the ``pgvector``
Postgres extension is present.

Per the C4 doc's "Configuration & Secrets" section: "the Worker verifies
the pgvector extension is present at startup, failing fast with a clear
error rather than surfacing an obscure query failure later." This module
is that check; a later phase calls it from the Worker's composition root
(``apps/worker/main.py``) and Phase 5's Docker Compose health check.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.storage.engine import redact_dsn
from adapter.storage.exceptions import PgVectorNotInstalledError, StorageConnectionError

logger = logging.getLogger(__name__)

_PGVECTOR_PRESENCE_QUERY = text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")


async def check_pgvector_installed(engine: AsyncEngine) -> None:
    """Raise ``PgVectorNotInstalledError`` if the connected database does
    not have the ``vector`` extension installed, or ``StorageConnectionError``
    if Postgres could not be reached at all.

    Deliberately checks presence only — it does not attempt
    ``CREATE EXTENSION`` itself. Enabling the extension is the baseline
    migration's job (``adapter/storage/migrations/versions``); this check
    exists so a misconfigured/unmigrated database fails fast with a typed
    error instead of an obscure query failure the first time later phases'
    vector columns are queried.

    Per the plan's Phase 1 section: genuinely extended this phase to catch
    a connection failure at ``engine.connect()`` too (previously this only
    ever wrapped the "extension not installed" case, which requires a
    connection to have already succeeded) — re-raised as
    ``StorageConnectionError`` rather than a raw driver exception, per
    architecture.md's "specific exception types" rule and "re-raise with
    ``raise ... from err`` to preserve the chain".
    """
    try:
        # ``await engine.connect()`` (rather than ``async with``) already
        # starts the connection here (``AsyncConnection.__await__`` calls
        # ``start()``) — an *additional* ``async with connection:`` below
        # would call ``start()`` a second time and raise
        # ``InvalidRequestError("connection is already started")``.
        # Cleanup is therefore a plain ``finally: await connection.close()``,
        # not a second context-manager entry.
        connection = await engine.connect()
    except Exception as exc:
        raise StorageConnectionError(dsn_summary=redact_dsn(str(engine.url))) from exc

    try:
        try:
            result = await connection.execute(_PGVECTOR_PRESENCE_QUERY)
            row = result.first()
        except Exception as exc:
            # Connectivity has already succeeded above — a failure here is a
            # query/permissions problem (e.g. no SELECT on pg_catalog), not
            # an unreachable database. Still surfaced as StorageConnectionError
            # (no narrower typed exception exists for this case), but with a
            # message that doesn't send an operator chasing connectivity that
            # was never the problem.
            raise StorageConnectionError(
                dsn_summary=redact_dsn(str(engine.url)),
                detail="connected successfully but the pgvector presence query failed",
            ) from exc
    finally:
        # A close-time failure on an already-broken connection (the same
        # break that likely caused the query above to fail) must not
        # replace whatever exception is already propagating out of the
        # try/except above — swallowed and logged, not raised, the same
        # way `adapter/analyzers/subprocess_runner.py`'s
        # `cleanup_temp_directory` treats its own teardown failures:
        # visible in logs, never masking the invocation's real outcome.
        try:
            await connection.close()
        except Exception as exc:
            logger.warning(
                "pgvector presence check: connection close failed",
                extra={"error": str(exc)},
            )

    if row is None:
        raise PgVectorNotInstalledError(dsn_summary=redact_dsn(str(engine.url)))
