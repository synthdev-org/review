"""apps.admin_api.bootstrap — the idempotent bootstrap-admin routine.

Deliberately bypasses ``iam.auth.UserRepository``/
``adapter/storage/iam.py``'s ``PostgresUserRepository`` entirely, unlike
every other write in this phase: this routine's whole reason for existing
is to hold a ``pg_advisory_xact_lock`` around a check-then-insert, closing
a real multi-replica TOCTOU race — ``apps/admin_api`` runs multiple
stateless replicas with no sticky-session requirement, so concurrent
replicas starting against a fresh database could otherwise each observe an
empty ``admin_user`` table and race to insert. ``UserRepository``'s own
Protocol has no way to express "hold this lock for the duration of this
check and this insert, in one transaction, using this exact SQL" without
leaking that requirement into a Protocol every other, ordinary caller of
``UserRepository.create`` (``POST /api/v1/users``) would then
also have to satisfy for no reason — so this module talks to
``adapter/storage/models.py``'s ``AdminUserRow``/SQLAlchemy directly
instead, the same "an entrypoint's own composition-root-adjacent code may
wire adapters/SQLAlchemy directly for a transaction-boundary concern no
shared Protocol covers" precedent
``apps/webhook_api/webhook_routes.py``'s own caller-supplied-``AsyncSession``
plumbing already establishes.

Password hashing happens **inside** the lock, only when actually
inserting — not unconditionally on every process startup — so a
steady-state restart (the overwhelmingly common case once a deployment is
bootstrapped) pays no Argon2 CPU cost at all, not just a cheap no-op query.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.argon2_hasher import Argon2PasswordHasher
from adapter.storage.models import AdminRole, AdminUserRow

logger = logging.getLogger(__name__)

# A fixed, arbitrary 63-bit key `pg_advisory_xact_lock` serializes every
# concurrent replica's bootstrap attempt against — Postgres's advisory-lock
# keyspace is process/application-chosen, not tied to any table/row, so
# this only needs to be a value this codebase doesn't use for any other
# advisory lock (verified: this is the only pg_advisory_* call site in this
# repo as of this phase).
_BOOTSTRAP_ADVISORY_LOCK_KEY = 7_312_024_891


class BootstrapPasswordTooShortError(Exception):
    """Raised when the operator-configured
    ``admin_bootstrap_password`` is shorter than ``min_password_length`` —
    the password-strength rule is enforced server-side before hashing,
    checked here (not left to Argon2 to hash an under-strength password
    without complaint) before the advisory lock is even acquired, so a
    misconfigured deployment fails loudly at startup rather than
    bootstrapping a weak credential.
    """


async def bootstrap_admin_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    username: str,
    password: str,
    password_hasher: Argon2PasswordHasher,
    min_password_length: int,
) -> None:
    """No-op if any ``admin_user`` row already exists (regardless of its
    username — US-A3's "already has at least one admin user -> no-op"
    guarantee, which a narrower ``ON CONFLICT (username) DO NOTHING`` alone
    would not satisfy for a differently-named later admin); otherwise
    inserts one ``admin`` row for ``username``/``password``. Safe to call
    from every replica at every startup — the advisory lock serializes the
    check-then-insert across concurrent replicas, so only the first to
    acquire it ever inserts, and every later replica's own check correctly
    observes that row and no-ops.

    Raises ``BootstrapPasswordTooShortError`` if ``password`` is shorter
    than ``min_password_length`` — checked on every call (cheap, pure), not
    only the first, so a misconfigured deployment fails the same way on
    every replica rather than only the one that happened to win the
    advisory lock race.
    """
    if len(password) < min_password_length:
        raise BootstrapPasswordTooShortError(
            f"admin_bootstrap_password is shorter than the configured "
            f"min_password_length ({min_password_length})"
        )

    async with session_factory() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BOOTSTRAP_ADVISORY_LOCK_KEY}
        )
        existing = (await session.execute(select(AdminUserRow.id).limit(1))).first()
        if existing is not None:
            logger.info("admin bootstrap skipped: an admin user already exists")
            return

        password_hash = await asyncio.to_thread(password_hasher.hash, password)
        session.add(
            AdminUserRow(
                username=username,
                password_hash=password_hash,
                role=AdminRole.ADMIN,
                is_active=True,
            )
        )
        logger.info("admin bootstrap: created initial admin user", extra={"username": username})
