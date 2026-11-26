"""adapter.storage.engine — async SQLAlchemy engine/session construction.

Per docs/rules/architecture.md's Decoupling section, this adapter never
reads the DSN from an environment variable itself — the DSN is bootstrap
config, parsed once via ``pydantic-settings`` at each entrypoint
(``apps/webhook_api/settings.py``, ``apps/worker/settings.py``) and passed
down explicitly to the functions here.
"""

from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_storage_engine(dsn: str, *, echo: bool = False) -> AsyncEngine:
    """Build the async engine used by every table in ``adapter.storage``.

    ``pool_pre_ping`` protects long-lived Worker/Webhook API processes from
    handing out a connection Postgres has already dropped.
    """
    return create_async_engine(dsn, echo=echo, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to ``engine``.

    ``expire_on_commit=False`` so a caller can still read attributes off a
    row (e.g. to map it to a pydantic model, see ``schemas.py``) after the
    transaction that fetched it has committed.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


def redact_dsn(dsn: str) -> str:
    """Return a credential-free summary of ``dsn`` safe to put in an error
    message or log line (host/port/database only), per
    docs/rules/architecture.md's "never appear in logs" credential rule.
    """
    url = make_url(dsn)
    return f"{url.drivername}://{url.host}:{url.port}/{url.database}"
