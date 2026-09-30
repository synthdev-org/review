"""Unit tests for iam.auth's pure logic (session-expiry math, role-check)
and its one async orchestration function (authenticate_session, exercised
with fakes for both Protocols), per docs/rules/testing.md's "new domain
logic a story touches still needs unit tests" rule and
docs/plans/plan-deliver-admin-gui.md's Phase 1 Tests section. No real I/O —
adapter/storage/iam.py's real Postgres-backed implementations are covered
by apps/admin_api's own integration tests instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from iam.auth import (
    SessionStore,
    UserRepository,
    authenticate_session,
    is_admin,
    is_session_expired,
)
from iam.models import Role, Session, User

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_IDLE_TIMEOUT_MINUTES = 30


def _session(*, session_id: str = "session-1", last_seen_at: datetime) -> Session:
    return Session(
        id=session_id,
        user_id="user-1",
        created_at=_NOW - timedelta(hours=1),
        last_seen_at=last_seen_at,
    )


def _user(*, user_id: str = "user-1", role: Role = "admin", is_active: bool = True) -> User:
    return User(
        id=user_id,
        username="alice",
        password_hash="irrelevant-for-these-tests",
        role=role,
        is_active=is_active,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _FakeSessionStore(SessionStore):
    """An in-memory fake implementing iam.auth.SessionStore, per
    docs/rules/testing.md's "fakes over mocks for adapters" convention."""

    def __init__(self, *, sessions: dict[str, Session] | None = None) -> None:
        self.sessions_by_token_hash = dict(sessions or {})
        self.touched_session_ids: list[str] = []
        self.revoked_session_ids: list[str] = []
        self.revoked_all_for_user_ids: list[str] = []

    async def create(self, *, user_id: str, token_hash: str, now: datetime) -> Session:
        session = Session(id=token_hash, user_id=user_id, created_at=now, last_seen_at=now)
        self.sessions_by_token_hash[token_hash] = session
        return session

    async def get_by_token_hash(self, *, token_hash: str) -> Session | None:
        return self.sessions_by_token_hash.get(token_hash)

    async def touch_last_seen(self, *, session_id: str, now: datetime) -> None:
        self.touched_session_ids.append(session_id)

    async def revoke(self, *, session_id: str) -> None:
        self.revoked_session_ids.append(session_id)

    async def revoke_all_for_user(self, *, user_id: str) -> None:
        self.revoked_all_for_user_ids.append(user_id)
        self.sessions_by_token_hash = {
            token_hash: session
            for token_hash, session in self.sessions_by_token_hash.items()
            if session.user_id != user_id
        }


class _FakeUserRepository(UserRepository):
    """An in-memory fake implementing iam.auth.UserRepository."""

    def __init__(self, *, users: dict[str, User] | None = None) -> None:
        self.users_by_id = dict(users or {})

    async def get_by_username(self, *, username: str) -> User | None:
        return next((u for u in self.users_by_id.values() if u.username == username), None)

    async def get_by_id(self, *, user_id: str) -> User | None:
        return self.users_by_id.get(user_id)

    async def create(self, *, username: str, password_hash: str, role: Role) -> User:
        raise NotImplementedError("not exercised by these tests")

    async def list_all(self) -> tuple[User, ...]:
        raise NotImplementedError("not exercised by these tests")

    async def deactivate(self, *, user_id: str) -> User | None:
        raise NotImplementedError("not exercised by these tests")


def test_session_within_idle_timeout_is_not_expired() -> None:
    session = _session(last_seen_at=_NOW - timedelta(minutes=10))

    assert is_session_expired(session, now=_NOW, idle_timeout_minutes=30) is False


def test_session_exactly_at_idle_timeout_boundary_is_not_expired() -> None:
    """A generous, not off-by-one-strict boundary — expiry requires
    strictly exceeding the configured window, per is_session_expired's own
    docstring."""
    session = _session(last_seen_at=_NOW - timedelta(minutes=30))

    assert is_session_expired(session, now=_NOW, idle_timeout_minutes=30) is False


def test_session_one_second_past_idle_timeout_is_expired() -> None:
    session = _session(last_seen_at=_NOW - timedelta(minutes=30, seconds=1))

    assert is_session_expired(session, now=_NOW, idle_timeout_minutes=30) is True


def test_freshly_touched_session_is_never_expired() -> None:
    session = _session(last_seen_at=_NOW)

    assert is_session_expired(session, now=_NOW, idle_timeout_minutes=30) is False


def test_is_admin_true_for_admin_role() -> None:
    assert is_admin(_user(role="admin")) is True


def test_is_admin_false_for_viewer_role() -> None:
    assert is_admin(_user(role="viewer")) is False


@pytest.mark.asyncio
async def test_authenticate_session_resolves_a_valid_session_and_touches_it() -> None:
    session = _session(last_seen_at=_NOW - timedelta(minutes=5))
    user = _user()
    session_store = _FakeSessionStore(sessions={"hash-1": session})
    user_repository = _FakeUserRepository(users={user.id: user})

    identity = await authenticate_session(
        token_hash="hash-1",
        session_store=session_store,
        user_repository=user_repository,
        now=_NOW,
        idle_timeout_minutes=_IDLE_TIMEOUT_MINUTES,
    )

    assert identity is not None
    assert identity.session.id == session.id
    assert identity.user.id == user.id
    assert session_store.touched_session_ids == [session.id]


@pytest.mark.asyncio
async def test_authenticate_session_returns_none_for_unknown_token() -> None:
    session_store = _FakeSessionStore()
    user_repository = _FakeUserRepository()

    identity = await authenticate_session(
        token_hash="no-such-hash",
        session_store=session_store,
        user_repository=user_repository,
        now=_NOW,
        idle_timeout_minutes=_IDLE_TIMEOUT_MINUTES,
    )

    assert identity is None
    assert session_store.touched_session_ids == []


@pytest.mark.asyncio
async def test_authenticate_session_never_touches_an_already_idle_expired_session() -> None:
    """The resurrection-prevention guarantee SessionStore's own docstring
    describes: an idle-expired session must never be touched, or the very
    next request would see a fresh last_seen_at and be wrongly accepted."""
    session = _session(last_seen_at=_NOW - timedelta(minutes=_IDLE_TIMEOUT_MINUTES, seconds=1))
    user = _user()
    session_store = _FakeSessionStore(sessions={"hash-1": session})
    user_repository = _FakeUserRepository(users={user.id: user})

    identity = await authenticate_session(
        token_hash="hash-1",
        session_store=session_store,
        user_repository=user_repository,
        now=_NOW,
        idle_timeout_minutes=_IDLE_TIMEOUT_MINUTES,
    )

    assert identity is None
    assert session_store.touched_session_ids == []


@pytest.mark.asyncio
async def test_authenticate_session_returns_none_for_a_deactivated_user() -> None:
    session = _session(last_seen_at=_NOW)
    user = _user(is_active=False)
    session_store = _FakeSessionStore(sessions={"hash-1": session})
    user_repository = _FakeUserRepository(users={user.id: user})

    identity = await authenticate_session(
        token_hash="hash-1",
        session_store=session_store,
        user_repository=user_repository,
        now=_NOW,
        idle_timeout_minutes=_IDLE_TIMEOUT_MINUTES,
    )

    assert identity is None
    assert session_store.touched_session_ids == []


@pytest.mark.asyncio
async def test_authenticate_session_returns_none_when_the_user_row_is_gone() -> None:
    session = _session(last_seen_at=_NOW)
    session_store = _FakeSessionStore(sessions={"hash-1": session})
    user_repository = _FakeUserRepository()  # empty: session.user_id resolves to nothing

    identity = await authenticate_session(
        token_hash="hash-1",
        session_store=session_store,
        user_repository=user_repository,
        now=_NOW,
        idle_timeout_minutes=_IDLE_TIMEOUT_MINUTES,
    )

    assert identity is None
