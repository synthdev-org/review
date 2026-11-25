"""Integration tests for apps/admin_api/bootstrap.py, per
docs/plans/plan-deliver-admin-gui.md's Phase 1 (Unit B) Tests section.

`test_concurrent_bootstrap_attempts_never_create_duplicate_admins` is the
plan's own required non-TestClient concurrency test proving the
`pg_advisory_xact_lock` (Assumption 11) actually prevents duplicate admins
under real concurrent contention — invoking `bootstrap_admin_user` directly
via `asyncio.gather` over two independently-created engines/session
factories against the same `migrated_engine` database, mirroring
`adapter/storage/test_queue_skip_locked.py`'s existing pattern for forcing
genuine concurrent contention. `TestClient`'s own `with`-block precedent
(used by every other integration test in this package) is single-portal
and sequential by design and cannot exercise two overlapping bootstrap
attempts on its own.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.auth.argon2_hasher import Argon2PasswordHasher
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.models import AdminUserRow
from apps.admin_api.bootstrap import BootstrapPasswordTooShortError, bootstrap_admin_user

pytestmark = pytest.mark.integration

_MIN_PASSWORD_LENGTH = 12


def _dsn(migrated_engine: AsyncEngine) -> str:
    return str(migrated_engine.url.render_as_string(hide_password=False))


@pytest.mark.asyncio
async def test_concurrent_bootstrap_attempts_never_create_duplicate_admins(
    migrated_engine: AsyncEngine,
) -> None:
    dsn = _dsn(migrated_engine)
    # Two independently-created engines/session factories against the same
    # database — the same "force genuine concurrent contention across
    # separate connections" shape
    # adapter/storage/test_queue_skip_locked.py's own concurrent-claimers
    # test uses, per this test's own module docstring.
    engine_a = create_storage_engine(dsn)
    engine_b = create_storage_engine(dsn)
    password_hasher = Argon2PasswordHasher()

    try:
        await asyncio.gather(
            bootstrap_admin_user(
                create_session_factory(engine_a),
                username="race-admin",
                password="a-sufficiently-long-password",
                password_hasher=password_hasher,
                min_password_length=_MIN_PASSWORD_LENGTH,
            ),
            bootstrap_admin_user(
                create_session_factory(engine_b),
                username="race-admin",
                password="a-sufficiently-long-password",
                password_hasher=password_hasher,
                min_password_length=_MIN_PASSWORD_LENGTH,
            ),
        )
    finally:
        await engine_a.dispose()
        await engine_b.dispose()

    session_factory = create_session_factory(migrated_engine)
    async with session_factory() as session:
        rows = (await session.execute(select(AdminUserRow))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_bootstrap_no_ops_when_an_admin_user_already_exists(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    password_hasher = Argon2PasswordHasher()

    await bootstrap_admin_user(
        session_factory,
        username="first-admin",
        password="a-sufficiently-long-password",
        password_hasher=password_hasher,
        min_password_length=_MIN_PASSWORD_LENGTH,
    )
    await bootstrap_admin_user(
        session_factory,
        username="second-admin",
        password="a-sufficiently-long-password",
        password_hasher=password_hasher,
        min_password_length=_MIN_PASSWORD_LENGTH,
    )

    async with session_factory() as session:
        rows = (await session.execute(select(AdminUserRow))).scalars().all()
        assert len(rows) == 1
        assert rows[0].username == "first-admin"


@pytest.mark.asyncio
async def test_bootstrap_raises_for_a_too_short_password(migrated_engine: AsyncEngine) -> None:
    session_factory = create_session_factory(migrated_engine)
    password_hasher = Argon2PasswordHasher()

    with pytest.raises(BootstrapPasswordTooShortError):
        await bootstrap_admin_user(
            session_factory,
            username="short-password-admin",
            password="short",
            password_hasher=password_hasher,
            min_password_length=_MIN_PASSWORD_LENGTH,
        )

    async with session_factory() as session:
        rows = (await session.execute(select(AdminUserRow))).scalars().all()
        assert len(rows) == 0
