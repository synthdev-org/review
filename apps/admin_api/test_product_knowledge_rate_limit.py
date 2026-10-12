"""Unit test: the test-connection limiters are checked together before either
is charged, so a global rejection never burns a session slot (plan M8). Driven
through the real router with a fake clock and stub dependencies (no I/O)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from apps.admin_api.dependencies import AuthenticatedSession
from apps.admin_api.product_knowledge_routes import (
    GLOBAL_LIMIT_PER_MINUTE,
    SESSION_LIMIT_PER_MINUTE,
    ProductKnowledgeRouterConfig,
    create_product_knowledge_router,
)
from iam.models import User


class _Audit:
    async def record(self, **_: Any) -> None:
        return None


def _session(sid: str, uid: str | None = None) -> AuthenticatedSession:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    user = User(
        id=f"user-{uid or sid}",  # distinct users: only the global window can trip
        username="admin",
        password_hash="x",
        role="admin",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    return AuthenticatedSession(session_id=sid, user=user)


def test_a_global_rejection_does_not_consume_the_sessions_own_slots() -> None:
    now = [0.0]

    async def require_session(x_sid: str = Header()) -> AuthenticatedSession:
        return _session(x_sid)

    app = FastAPI()
    app.include_router(
        create_product_knowledge_router(
            config=ProductKnowledgeRouterConfig(
                credentials=None,  # answers `disabled` once past the limiters
                enabled=True,
                endpoint_allowlist=(),
                probe_factory=lambda url, ref: (_ for _ in ()).throw(AssertionError("no probe")),
            ),
            audit_log_repository=_Audit(),  # type: ignore[arg-type]
            require_session=require_session,  # type: ignore[arg-type]
            require_admin_role=require_session,
            clock=lambda: now[0],
        )
    )
    client = TestClient(app)

    def post(sid: str) -> int:
        return client.post(
            "/api/v1/product-knowledge/test-connection",
            json={"endpoint_url": "https://wiki.example.com/mcp"},
            headers={"x-sid": sid},
        ).status_code

    for n in range(GLOBAL_LIMIT_PER_MINUTE // SESSION_LIMIT_PER_MINUTE):  # fill the global window
        assert [post(f"s{n}") for _ in range(SESSION_LIMIT_PER_MINUTE)] == [200] * 6
    now[0] = 30.0
    assert [post("victim") for _ in range(SESSION_LIMIT_PER_MINUTE)] == [429] * 6
    now[0] = 61.0  # the global window has drained; the victim never used a slot

    assert post("victim") == 200


def test_session_limiter_trips_independently_of_the_user_limiter() -> None:
    """Two users share one session id: neither user is near the per-user limit
    (3 each), yet the session's own window rejects the 7th request."""

    async def require_session(x_sid: str = Header(), x_uid: str = Header()) -> AuthenticatedSession:
        return _session(x_sid, x_uid)

    app = FastAPI()
    app.include_router(
        create_product_knowledge_router(
            config=ProductKnowledgeRouterConfig(
                credentials=None,
                enabled=True,
                endpoint_allowlist=(),
                probe_factory=lambda url, ref: (_ for _ in ()).throw(AssertionError("no probe")),
            ),
            audit_log_repository=_Audit(),  # type: ignore[arg-type]
            require_session=require_session,  # type: ignore[arg-type]
            require_admin_role=require_session,
            clock=lambda: 0.0,
        )
    )
    client = TestClient(app)

    def post(sid: str, uid: str) -> int:
        return client.post(
            "/api/v1/product-knowledge/test-connection",
            json={"endpoint_url": "https://wiki.example.com/mcp"},
            headers={"x-sid": sid, "x-uid": uid},
        ).status_code

    half = SESSION_LIMIT_PER_MINUTE // 2
    assert [post("shared", "alice") for _ in range(half)] == [200] * half
    assert [post("shared", "bob") for _ in range(half)] == [200] * half
    assert post("shared", "alice") == 429  # session window full, alice has used only 3
    assert post("other-session", "alice") == 200  # her own user window still has room
