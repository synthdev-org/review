"""adapter.storage.iam — Postgres-backed implementations of ``iam.auth``'s
two storage Protocols (``UserRepository``, ``SessionStore``), plus the two
concrete, no-Protocol repositories Assumption 1/5 call for
(``PostgresAuditLogRepository``, ``PostgresLoginAttemptRepository``), per
``docs/plans/plan-deliver-admin-gui.md``'s Phase 1 section (Unit B).

Every method opens (and, for a write, commits) its own session via
``adapter/storage/unit_of_work.py``'s ``run_in_session`` — the same
``JobQueueRepository``/``PostgresTenancyStore`` shape, with no caller-
supplied ``unit_of_work`` support: unlike the webhook receiver's shared-
transaction needs, nothing in this plan asks two of these repositories'
writes to commit atomically together (a login's rate-limit check and its
audit-log write are two independent, sequential steps, not one transaction
— a rate-limit write succeeding while an audit write fails is an accepted,
logged gap, not a correctness bug this plan's own security posture depends
on avoiding).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.storage.models import (
    AdminAuditLogRow,
    AdminRole,
    AdminSessionRow,
    AdminUserRow,
    LoginAttemptRow,
)
from adapter.storage.unit_of_work import run_in_session
from iam.auth import UsernameAlreadyExistsError
from iam.models import Role, Session, User

# How many login_attempt windows' worth of history
# PostgresLoginAttemptRepository.record_failed_attempt purges on every
# write, per the plan's Phase 1 section ("a periodic purge of rows older
# than a few login_rate_limit_window_minutes windows"). A plain multiple of
# the configured window, not a separately-configurable setting of its own
# — this is retention hygiene, not a tunable rate-limit parameter.
_PURGE_WINDOW_MULTIPLE = 4


def _user_from_row(row: AdminUserRow) -> User:
    return User(
        id=str(row.id),
        username=row.username,
        password_hash=row.password_hash,
        role=row.role.value,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _session_from_row(row: AdminSessionRow) -> Session:
    return Session(
        id=str(row.id),
        user_id=str(row.user_id),
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
    )


class PostgresUserRepository:
    """Implements ``iam.auth.UserRepository`` against the ``admin_user``
    table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_by_username(self, *, username: str) -> User | None:
        async def _read(session: AsyncSession) -> User | None:
            row = (
                await session.execute(select(AdminUserRow).where(AdminUserRow.username == username))
            ).scalar_one_or_none()
            return _user_from_row(row) if row is not None else None

        return await run_in_session(self._session_factory, None, _read)

    async def get_by_id(self, *, user_id: str) -> User | None:
        async def _read(session: AsyncSession) -> User | None:
            row = (
                await session.execute(
                    select(AdminUserRow).where(AdminUserRow.id == uuid.UUID(user_id))
                )
            ).scalar_one_or_none()
            return _user_from_row(row) if row is not None else None

        return await run_in_session(self._session_factory, None, _read)

    async def create(self, *, username: str, password_hash: str, role: Role) -> User:
        async def _write(session: AsyncSession) -> User:
            row = AdminUserRow(username=username, password_hash=password_hash, role=AdminRole(role))
            session.add(row)
            try:
                await session.flush()
            except IntegrityError as exc:
                # admin_user's own uq_admin_user_username constraint
                # (Assumption 11) — translated to a business-level
                # exception here rather than left as a raw
                # sqlalchemy.exc.IntegrityError, per
                # docs/rules/architecture.md's exception-type rule.
                raise UsernameAlreadyExistsError(username=username) from exc
            return _user_from_row(row)

        return await run_in_session(self._session_factory, None, _write)

    async def list_all(self) -> tuple[User, ...]:
        async def _read(session: AsyncSession) -> tuple[User, ...]:
            rows = (
                (await session.execute(select(AdminUserRow).order_by(AdminUserRow.created_at)))
                .scalars()
                .all()
            )
            return tuple(_user_from_row(row) for row in rows)

        return await run_in_session(self._session_factory, None, _read)

    async def deactivate(self, *, user_id: str) -> User | None:
        try:
            parsed_user_id = uuid.UUID(user_id)
        except ValueError:
            # Malformed input, not a real id — treated as "not found",
            # matching this Protocol's own "never raises for the
            # not-found case" contract (get_by_username's docstring).
            return None

        async def _write(session: AsyncSession) -> User | None:
            row = (
                await session.execute(select(AdminUserRow).where(AdminUserRow.id == parsed_user_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            row.is_active = False
            await session.flush()
            # updated_at's onupdate=func.now() is server-computed on the
            # UPDATE this flush just issued — refresh so _user_from_row
            # reads the real new value rather than triggering an
            # unexpected lazy-load later (mirrors
            # adapter/storage/memory_repository.py's set_status, the same
            # "mutate then refresh before returning" shape for a row with
            # a server-side onupdate column).
            await session.refresh(row)
            return _user_from_row(row)

        return await run_in_session(self._session_factory, None, _write)


class PostgresSessionStore:
    """Implements ``iam.auth.SessionStore`` against the ``admin_session``
    table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, *, user_id: str, token_hash: str, now: datetime) -> Session:
        async def _write(session: AsyncSession) -> Session:
            row = AdminSessionRow(
                user_id=uuid.UUID(user_id),
                token_hash=token_hash,
                created_at=now,
                last_seen_at=now,
            )
            session.add(row)
            await session.flush()
            return _session_from_row(row)

        return await run_in_session(self._session_factory, None, _write)

    async def get_by_token_hash(self, *, token_hash: str) -> Session | None:
        async def _read(session: AsyncSession) -> Session | None:
            row = (
                await session.execute(
                    select(AdminSessionRow).where(AdminSessionRow.token_hash == token_hash)
                )
            ).scalar_one_or_none()
            return _session_from_row(row) if row is not None else None

        return await run_in_session(self._session_factory, None, _read)

    async def touch_last_seen(self, *, session_id: str, now: datetime) -> None:
        async def _write(session: AsyncSession) -> None:
            row = (
                await session.execute(
                    select(AdminSessionRow).where(AdminSessionRow.id == uuid.UUID(session_id))
                )
            ).scalar_one_or_none()
            if row is None:
                # Benign race against a concurrent logout/revoke — see this
                # method's own Protocol docstring.
                return
            row.last_seen_at = now

        await run_in_session(self._session_factory, None, _write)

    async def revoke(self, *, session_id: str) -> None:
        async def _write(session: AsyncSession) -> None:
            await session.execute(
                delete(AdminSessionRow).where(AdminSessionRow.id == uuid.UUID(session_id))
            )

        await run_in_session(self._session_factory, None, _write)

    async def revoke_all_for_user(self, *, user_id: str) -> None:
        try:
            parsed_user_id = uuid.UUID(user_id)
        except ValueError:
            # Malformed input, not a real id — a no-op, matching this
            # method's own "no-op if user_id currently has no sessions"
            # contract (there are, by construction, none for an id that
            # isn't even a well-formed uuid).
            return

        async def _write(session: AsyncSession) -> None:
            await session.execute(
                delete(AdminSessionRow).where(AdminSessionRow.user_id == parsed_user_id)
            )

        await run_in_session(self._session_factory, None, _write)


class PostgresAuditLogRepository:
    """Writes one row to the shared ``admin_audit_log`` table (Assumption
    6) — a concrete class, no Protocol (no second implementation in view,
    the same reasoning Assumption 1 already states for
    ``Argon2PasswordHasher``/the login rate limiter below).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        *,
        actor_user_id: str | None,
        action: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        async def _write(session: AsyncSession) -> None:
            session.add(
                AdminAuditLogRow(
                    actor_user_id=uuid.UUID(actor_user_id) if actor_user_id is not None else None,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    extra=metadata or {},
                )
            )

        await run_in_session(self._session_factory, None, _write)


class PostgresLoginAttemptRepository:
    """The Postgres-backed windowed login-rate-limit counter (Assumption
    5) — a concrete class, no Protocol, per that Assumption's own
    reasoning. Fixed-window (not sliding), keyed by ``(username,
    window_start)``; ``window_start`` is the configured window's own
    epoch-aligned floor of the timestamp passed in, so every call within
    the same window resolves to the same row.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_attempts: int,
        window_minutes: int,
    ) -> None:
        self._session_factory = session_factory
        self._max_attempts = max_attempts
        self._window_minutes = window_minutes

    def _window_start(self, now: datetime) -> datetime:
        window_seconds = self._window_minutes * 60
        epoch_seconds = now.timestamp()
        floored_epoch_seconds = epoch_seconds - (epoch_seconds % window_seconds)
        return datetime.fromtimestamp(floored_epoch_seconds, tz=now.tzinfo)

    async def is_rate_limited(self, *, username: str, now: datetime) -> bool:
        """Read-only check — does **not** itself count as an attempt (no
        increment), so a ceiling-exceeded caller can be rejected before
        this module ever runs an Argon2 verification, per this phase's
        own Security review ("brute force against one username").
        """
        window_start = self._window_start(now)

        async def _read(session: AsyncSession) -> int:
            row = (
                await session.execute(
                    select(LoginAttemptRow.attempt_count).where(
                        LoginAttemptRow.username == username,
                        LoginAttemptRow.window_start == window_start,
                    )
                )
            ).scalar_one_or_none()
            return row or 0

        attempt_count = await run_in_session(self._session_factory, None, _read)
        return attempt_count >= self._max_attempts

    async def record_failed_attempt(self, *, username: str, now: datetime) -> None:
        """Atomically increments ``username``'s counter for the current
        window (``INSERT ... ON CONFLICT DO UPDATE attempt_count =
        attempt_count + 1``, an atomic-upsert pattern), then purges rows
        older than ``_PURGE_WINDOW_MULTIPLE`` windows — bounding this
        table's growth against a flood of attempts against nonexistent
        usernames, per this phase's own Security review.
        """
        window_start = self._window_start(now)
        purge_before = window_start - timedelta(
            minutes=self._window_minutes * _PURGE_WINDOW_MULTIPLE
        )

        async def _write(session: AsyncSession) -> None:
            insert_statement = pg_insert(LoginAttemptRow).values(
                username=username, window_start=window_start, attempt_count=1
            )
            await session.execute(
                insert_statement.on_conflict_do_update(
                    index_elements=["username", "window_start"],
                    set_={"attempt_count": LoginAttemptRow.attempt_count + 1},
                )
            )
            await session.execute(
                delete(LoginAttemptRow).where(LoginAttemptRow.window_start < purge_before)
            )

        await run_in_session(self._session_factory, None, _write)
