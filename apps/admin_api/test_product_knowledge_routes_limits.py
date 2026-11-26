"""
Integration tests for ``product_knowledge_routes.py``: audit, limits, credentials.

See ``pk_routes_harness`` for the shared app/provider harness (A2).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.product_knowledge.testing.fake_provider import (
    FakeProvider,
)
from apps.admin_api.dependencies import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from apps.admin_api.probe_rate_limit import (
    GLOBAL_LIMIT_PER_MINUTE,
    SESSION_LIMIT_PER_MINUTE,
)
from apps.admin_api.testing.pk_routes_harness import (
    HOST,
    TOKEN,
    Harness,
    audit_rows,
    dsn,
    login,
    post,
    run,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# --- audit -----------------------------------------------------------------------


async def test_every_probe_is_audited_with_actor_host_and_status_never_a_token(
    migrated_engine: AsyncEngine,
) -> None:
    database_url = dsn(migrated_engine)
    async with FakeProvider(token=TOKEN) as provider:
        h = Harness(database_url, provider)

        def drive(c: TestClient, csrf: str) -> None:
            post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
            post(c, csrf, endpoint_url=f"http://scan-me.internal:{provider.port}/mcp")
            post(c, csrf, endpoint_url=h.endpoint, credential_ref="nope")

        await run(h, drive)

    rows = sorted(await audit_rows(database_url), key=lambda r: r.created_at)
    assert [r.extra["status"] for r in rows] == [
        "ok",
        "blocked_by_policy",
        "unknown_credential_ref",
    ]
    assert [r.extra["endpoint_host"] for r in rows] == [HOST, "scan-me.internal", HOST]
    assert rows[0].extra["credential_ref"] == "wiki"
    assert all(r.actor_user_id is not None for r in rows)
    assert TOKEN not in str([r.extra for r in rows])


# --- rate limits -----------------------------------------------------------------


async def test_session_limit_returns_429_after_six_calls_a_minute(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider() as provider:
        h = Harness(dsn(migrated_engine), provider)

        def drive(c: TestClient, csrf: str) -> list[int]:
            return [
                post(c, csrf, endpoint_url=f"http://scan.internal:{provider.port}/mcp").status_code
                for _ in range(SESSION_LIMIT_PER_MINUTE + 1)
            ]

        statuses = await run(h, drive)

    assert statuses == [200] * SESSION_LIMIT_PER_MINUTE + [429]
    assert h.bytes_sent == 0


async def test_global_limit_returns_429_across_users(migrated_engine: AsyncEngine) -> None:
    async with FakeProvider() as provider:
        h = Harness(dsn(migrated_engine), provider)
        users_needed = GLOBAL_LIMIT_PER_MINUTE // SESSION_LIMIT_PER_MINUTE

        def drive() -> tuple[list[int], int]:
            # Distinct users, so neither the per-session nor the per-user
            # limit trips: only the global window can produce the 429.
            names = [f"pkuser{i}" for i in range(users_needed + 1)]
            ok: list[int] = []
            with TestClient(h.app) as client:
                csrf = login(client)
                for name in names:
                    created = client.post(
                        "/api/v1/users",
                        json={
                            "username": name,
                            "password": "Correct-Horse-9-Battery",
                            "role": "admin",
                        },
                        headers={CSRF_HEADER_NAME: csrf},
                    )
                    assert created.status_code == 201, created.text
                statuses: list[int] = []
                for name in names:
                    client.cookies.clear()
                    client.post(
                        "/api/v1/auth/login",
                        json={"username": name, "password": "Correct-Horse-9-Battery"},
                    )
                    user_csrf = str(client.cookies[CSRF_COOKIE_NAME])
                    count = 1 if name == names[-1] else SESSION_LIMIT_PER_MINUTE
                    statuses.extend(
                        post(
                            client,
                            user_csrf,
                            endpoint_url=f"http://scan.internal:{provider.port}/mcp",
                        ).status_code
                        for _ in range(count)
                    )
                ok = statuses[:-1]
            return ok, statuses[-1]

        ok, over = await asyncio.to_thread(drive)

    assert ok == [200] * GLOBAL_LIMIT_PER_MINUTE
    assert over == 429


async def test_the_per_user_limit_survives_fresh_logins(migrated_engine: AsyncEngine) -> None:
    """L1: logging in again (new session id) must not reset the quota."""
    async with FakeProvider() as provider:
        h = Harness(dsn(migrated_engine), provider)

        def drive() -> tuple[list[int], int]:
            ok: list[int] = []
            with TestClient(h.app) as client:
                for _ in range(2):  # two sessions, same user
                    csrf = login(client)
                    ok.extend(
                        post(
                            client, csrf, endpoint_url=f"http://scan.internal:{provider.port}/mcp"
                        ).status_code
                        for _ in range(SESSION_LIMIT_PER_MINUTE // 2)
                    )
                csrf = login(client)  # third session, user already at its limit
                over = post(
                    client, csrf, endpoint_url=f"http://scan.internal:{provider.port}/mcp"
                ).status_code
            return ok, over

        ok, over = await asyncio.to_thread(drive)

    assert ok == [200] * SESSION_LIMIT_PER_MINUTE
    assert over == 429


# --- GET /credentials ---------------------------------------------------------------


async def test_credentials_list_shows_names_always_and_hosts_only_to_admins_never_tokens(
    migrated_engine: AsyncEngine,
) -> None:
    database_url = dsn(migrated_engine)
    async with FakeProvider() as provider:
        h = Harness(database_url, provider)
        admin_body = await run(h, lambda c, csrf: c.get("/api/v1/product-knowledge/credentials"))

    assert admin_body.status_code == 200
    assert admin_body.json() == [{"ref": "wiki", "allowed_hosts": [HOST]}]
    assert TOKEN not in admin_body.text


async def test_credentials_list_requires_a_session(migrated_engine: AsyncEngine) -> None:
    async with FakeProvider() as provider:
        h = Harness(dsn(migrated_engine), provider)

        def drive() -> int:
            with TestClient(h.app) as client:
                return int(client.get("/api/v1/product-knowledge/credentials").status_code)

        status = await asyncio.to_thread(drive)

    assert status == 401


async def test_test_connection_requires_a_session_and_a_csrf_token(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=TOKEN) as provider:
        h = Harness(dsn(migrated_engine), provider)
        body = {"endpoint_url": h.endpoint, "credential_ref": "wiki"}

        def drive() -> tuple[int, int]:
            with TestClient(h.app) as client:
                anonymous = client.post("/api/v1/product-knowledge/test-connection", json=body)
                login(client)  # session cookie set, CSRF header deliberately omitted
                no_csrf = client.post("/api/v1/product-knowledge/test-connection", json=body)
            return anonymous.status_code, no_csrf.status_code

        statuses = await asyncio.to_thread(drive)

    assert statuses == (401, 403)
    assert h.probe_calls == []
    assert h.bytes_sent == 0
