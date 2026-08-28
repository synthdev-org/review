"""adapter.storage.pgvector_check — startup check that the ``pgvector``
Postgres extension is present.

Per the C4 doc's "Configuration & Secrets" section: "the Worker verifies
the pgvector extension is present at startup, failing fast with a clear
error rather than surfacing an obscure query failure later." This module
is that check; a later phase calls it from the Worker's composition root
(``apps/worker/main.py``) and Phase 5's Docker Compose health check.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.storage.engine import redact_dsn
from adapter.storage.exceptions import PgVectorNotInstalledError

_PGVECTOR_PRESENCE_QUERY = text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")


async def check_pgvector_installed(engine: AsyncEngine) -> None:
    """Raise ``PgVectorNotInstalledError`` if the connected database does
    not have the ``vector`` extension installed.

    Deliberately checks presence only — it does not attempt
    ``CREATE EXTENSION`` itself. Enabling the extension is the baseline
    migration's job (``adapter/storage/migrations/versions``); this check
    exists so a misconfigured/unmigrated database fails fast with a typed
    error instead of an obscure query failure the first time later phases'
    vector columns are queried.
    """
    async with engine.connect() as connection:
        result = await connection.execute(_PGVECTOR_PRESENCE_QUERY)
        if result.first() is None:
            raise PgVectorNotInstalledError(dsn_summary=redact_dsn(str(engine.url)))
