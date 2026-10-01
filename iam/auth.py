"""iam.auth — pure session-expiry/role-check logic plus the two storage
Protocols this bounded context needs (``UserRepository``, ``SessionStore``),
per ``docs/plans/plan-deliver-admin-gui.md``'s Phase 1 section (Unit B,
Assumption 1).

``is_session_expired``/``is_admin`` are pure functions over already-loaded
``iam.models`` instances — no I/O, no clock reads of their own (``now`` is
always caller-supplied) — so ``apps/admin_api/dependencies.py``'s
``require_session``/``require_admin_role`` can unit-test the actual
expiry/role decision against fakes for the two Protocols below, per this
phase's own Tests section, rather than only exercising it indirectly
through a real Postgres-backed integration test.

Both Protocols are implemented by ``adapter/storage/iam.py``'s
``PostgresUserRepository``/``PostgresSessionStore`` — two separate classes
(unlike ``memory.store``'s "one concrete class implements every Protocol"
precedent), since a user row and a session row are genuinely different
storage concerns with no shared query shape, not an arbitrary split.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from iam.models import AuthenticatedIdentity, Role, Session, User


def is_session_expired(session: Session, *, now: datetime, idle_timeout_minutes: int) -> bool:
    """US-A2's sliding-window idle expiry: ``True`` once more than
    ``idle_timeout_minutes`` has elapsed since ``session.last_seen_at`` —
    the last time an authenticated request against this session was
    actually served (updated by ``SessionStore.touch_last_seen``, never by
    this function itself). A session exactly at the boundary
    (``elapsed_minutes == idle_timeout_minutes``) is **not** expired —
    expiry requires strictly exceeding the configured window, the same
    "generous, not off-by-one-strict" boundary convention
    ``tenancy.usage``'s day-boundary helpers (a later phase) use.
    """
    elapsed_seconds = (now - session.last_seen_at).total_seconds()
    return elapsed_seconds > idle_timeout_minutes * 60


def is_admin(user: User) -> bool:
    """US-A5's role check: ``True`` only for an ``admin`` role — a
    ``viewer`` (this plan's only other role) always returns ``False``,
    never inferred from anything but the stored ``role`` field itself.
    """
    return user.role == "admin"


class UsernameAlreadyExistsError(Exception):
    """Raised by ``UserRepository.create`` when ``username`` already exists
    — a distinct, business-level outcome, never conflated with a generic
    storage failure, per ``docs/rules/architecture.md``'s exception-type
    rule (the same "specific exception types per domain concern" contract
    ``memory.store.MemoryEntryNotFoundError`` already establishes).
    ``apps/admin_api/user_routes.py``'s ``POST /api/v1/users`` (Phase 2,
    Unit C) translates this into an HTTP 409.
    """

    def __init__(self, *, username: str) -> None:
        super().__init__(f"username {username!r} already exists")
        self.username = username


async def authenticate_session(
    *,
    token_hash: str,
    session_store: SessionStore,
    user_repository: UserRepository,
    now: datetime,
    idle_timeout_minutes: int,
) -> AuthenticatedIdentity | None:
    """Resolve a hashed session token to its still-valid session and active
    user, applying every one of Assumption 7's checks in the one order
    that avoids the "any request resurrects an expired session" bug
    ``SessionStore``'s own docstring warns about: token known, then not
    idle-expired, then its user still active — only then is
    ``touch_last_seen`` called.

    ``apps/admin_api/dependencies.py``'s ``require_session`` FastAPI
    dependency is this function's one real caller — it owns translating a
    ``None`` result into a generic 401 (deliberately losing which specific
    check failed, matching US-A1's own "reveals nothing" posture at the
    HTTP boundary) and any reason-specific logging, neither of which this
    function does itself; this function's own job is purely the
    session-then-user resolution sequence, independently unit-testable
    against fakes for both Protocols with no HTTP/FastAPI import at all.
    """
    session = await session_store.get_by_token_hash(token_hash=token_hash)
    if session is None:
        return None

    if is_session_expired(session, now=now, idle_timeout_minutes=idle_timeout_minutes):
        return None

    user = await user_repository.get_by_id(user_id=session.user_id)
    if user is None or not user.is_active:
        return None

    await session_store.touch_last_seen(session_id=session.id, now=now)
    return AuthenticatedIdentity(session=session, user=user)


class UserRepository(Protocol):
    """Storage boundary for ``admin_user`` rows — implemented by
    ``adapter/storage/iam.py``'s ``PostgresUserRepository``. Five methods —
    two over ``docs/rules/architecture.md``'s usual 1-3 guideline, the same
    kind of stated exception ``SessionStore`` below already takes for its
    own four: ``get_by_username`` (login), ``get_by_id`` (re-resolving the
    acting user on every authenticated request, so a role/active-status
    change is honored on the very next request rather than only at next
    login), ``create`` (Phase 1's own bootstrap routine goes around this
    Protocol entirely — see ``apps/admin_api/bootstrap.py``'s own
    docstring for why — but Phase 2's ``POST /api/v1/users`` route is this
    method's real first caller), and Phase 2's own ``list_all``/
    ``deactivate`` (Unit C, US-A4) — one repository, one real consumer
    (``apps/admin_api/user_routes.py``), so splitting these two into a
    second Protocol would only add ceremony for no swappability gain.
    """

    async def get_by_username(self, *, username: str) -> User | None:
        """The ``admin_user`` row for ``username``, or ``None`` if no such
        user exists — never raises for the not-found case (US-A1's own
        "reveals nothing about whether the username exists" requirement
        starts here: the caller decides what a ``None`` result means,
        rather than this method distinguishing "not found" from "found"
        via an exception a caller could accidentally let leak into a
        response).
        """
        ...

    async def get_by_id(self, *, user_id: str) -> User | None:
        """The ``admin_user`` row for ``user_id``, or ``None`` if no such
        user exists (or, per Phase 2, never happens for a still-valid
        session — a deactivated user's sessions are revoked outright, not
        left to be caught by a live ``is_active`` check here every
        request).
        """
        ...

    async def create(self, *, username: str, password_hash: str, role: Role) -> User:
        """Insert a new ``admin_user`` row. ``password_hash`` is always an
        already-computed Argon2id hash (``adapter/auth/argon2_hasher.py``)
        — this Protocol never receives a plaintext password, per
        ``docs/rules/architecture.md``'s "domain packages have zero infra
        imports" rule (Argon2 hashing lives in ``adapter/auth``, a
        different adapter package this one has no reason to import).

        Raises ``UsernameAlreadyExistsError`` if ``username`` already
        exists (Phase 2's ``POST /api/v1/users``, translated to an HTTP
        409) — never a raw storage-layer constraint-violation exception.
        """
        ...

    async def list_all(self) -> tuple[User, ...]:
        """Every ``admin_user`` row, oldest first (Phase 2's
        ``GET /api/v1/users``, Unit C — this Protocol's only consumer, and
        a small, operator-managed table with no pagination need)."""
        ...

    async def deactivate(self, *, user_id: str) -> User | None:
        """Flips ``is_active`` to ``False`` for ``user_id`` (Phase 2's
        ``PATCH /api/v1/users/{user_id}/deactivate``, Unit C) — a status
        flip, never a hard delete, so every ``admin_audit_log`` row already
        attributed to this user (``actor_user_id``, no
        ``ON DELETE CASCADE``, per Assumption 6) keeps resolving to their
        username afterward (US-A4's second Given/When/Then). Returns the
        updated ``User``, or ``None`` if no such user exists (translated to
        an HTTP 404 by the route) — never raises for the not-found case,
        the same "caller decides what missing means" contract
        ``get_by_username`` above establishes.
        """
        ...


class SessionStore(Protocol):
    """Storage boundary for ``admin_session`` rows — implemented by
    ``adapter/storage/iam.py``'s ``PostgresSessionStore``.

    Five methods — two over ``docs/rules/architecture.md``'s usual 1-3
    guideline — because this plan's own idle-expiry design (Assumption 7)
    genuinely needs distinct single-purpose operations with different call
    sites and different error semantics: ``create`` (login),
    ``get_by_token_hash`` (read-only lookup, deliberately *not* combined
    with the touch below), ``touch_last_seen`` (called only after
    ``is_session_expired`` above has already judged the session still
    valid), ``revoke`` (logout, one session), and Phase 2's own
    ``revoke_all_for_user`` (Unit C, US-A4's second Given/When/Then —
    deactivation revokes every one of that user's sessions, not just the
    one the deactivating admin happens to be using). Combining
    ``get_by_token_hash`` and ``touch_last_seen`` into one read-and-touch
    method would touch (and thereby silently resurrect) a session this same
    call is about to reject as expired — ``apps/admin_api/dependencies.py``'s
    ``require_session`` relies on the touch happening strictly after, not
    as part of, the expiry check for exactly this reason.
    """

    async def create(self, *, user_id: str, token_hash: str, now: datetime) -> Session:
        """Insert a new session row for a just-authenticated user.
        ``token_hash`` is the SHA-256 hex digest of a fresh, high-entropy
        opaque token generated by the caller (``apps/admin_api/auth_routes.py``)
        — this Protocol never receives or returns the raw token itself,
        mirroring ``admin_api_key``'s own "never store or compare the raw
        secret" precedent (Assumption 7).
        """
        ...

    async def get_by_token_hash(self, *, token_hash: str) -> Session | None:
        """The session row matching ``token_hash``, or ``None`` if no
        session was ever created with it (or it was since revoked/deleted)
        — never raises for the not-found case, the same "caller decides
        what missing means" contract ``UserRepository.get_by_username``
        establishes above.
        """
        ...

    async def touch_last_seen(self, *, session_id: str, now: datetime) -> None:
        """Advance ``last_seen_at`` to ``now`` for the still-valid session
        ``session_id`` — the sliding-window update US-A2's idle expiry
        depends on. A no-op (not an error) if ``session_id`` no longer
        exists (a benign race against a concurrent logout/revoke)."""
        ...

    async def revoke(self, *, session_id: str) -> None:
        """Permanently invalidate ``session_id`` (US-A2's explicit logout)
        — a subsequent ``get_by_token_hash`` for the same session's token
        must return ``None``. A no-op (not an error) if ``session_id`` no
        longer exists.
        """
        ...

    async def revoke_all_for_user(self, *, user_id: str) -> None:
        """Permanently invalidates every ``admin_session`` row belonging to
        ``user_id`` (Phase 2's deactivation route, Unit C, US-A4's second
        Given/When/Then: "that user's existing sessions are revoked and
        they can no longer log in") — every one of that user's session
        tokens is rejected by the very next ``require_session`` call, the
        same way an ordinary expired/unknown session already is. A no-op
        (not an error) if ``user_id`` currently has no sessions.
        """
        ...
