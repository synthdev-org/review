"""Integration tests for ``product_knowledge_routes.py``: A2 ordering and kill switch.

See ``pk_routes_harness`` for the shared app/provider harness (plan
``plan-deliver-product-knowledge.md`` Phase 6 / 6b; A2).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.product_knowledge.testing.fake_provider import (
    FakeProvider,
)
from apps.admin_api.pk_routes_harness import (
    HOST,
    TOKEN,
    Harness,
    dsn,
    post,
    run,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# --- A2 ordering: the binding check is first and sends zero bytes ------------


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"credential_ref": "nope"}, "unknown_credential_ref"),
        ({"endpoint_url": "http://evil.test:{port}/mcp"}, "credential_host_not_bound"),
        ({"endpoint_url": "http://wiki.test.evil.test:{port}/mcp"}, "credential_host_not_bound"),
        ({"endpoint_url": "http://evilwiki.test:{port}/mcp"}, "credential_host_not_bound"),
    ],
)
async def test_binding_mismatch_is_422_and_sends_zero_bytes(
    migrated_engine: AsyncEngine, body: dict[str, str], code: str
) -> None:
    async with FakeProvider(token=TOKEN) as provider:
        h = Harness(dsn(migrated_engine), provider)
        payload: dict[str, str | None] = {"endpoint_url": h.endpoint, "credential_ref": "wiki"}
        payload.update({k: v.format(port=provider.port) for k, v in body.items()})

        response = await run(h, lambda c, csrf: post(c, csrf, **payload))

    assert response.status_code == 422
    assert response.json()["detail"] == code
    assert h.probe_calls == []  # the adapter was never even constructed
    assert h.bytes_sent == 0
    assert h.pinned_hosts == []


async def test_an_unprovisioned_target_gets_the_collapsed_status_and_no_timing(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider() as provider:
        h = Harness(dsn(migrated_engine), provider)

        response = await run(
            h,
            lambda c, csrf: post(
                c, csrf, endpoint_url=f"http://scan-me.internal:{provider.port}/mcp"
            ),
        )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "blocked_by_policy"
    assert body["latency_ms"] is None and body["tools_present"] is None
    assert h.probe_calls == []
    assert h.bytes_sent == 0


async def test_an_unauthenticated_provider_is_testable_when_its_host_is_allow_listed(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider() as provider:
        h = Harness(
            dsn(migrated_engine),
            provider,
            product_knowledge_endpoint_allowlist="open.test",
        )

        response = await run(
            h,
            lambda c, csrf: post(
                c, csrf, endpoint_url=f"http://open.test:{provider.port}/mcp", credential_ref=None
            ),
        )

    assert response.json()["status"] == "ok"
    assert {r.authorization for r in provider.requests} == {None}


async def test_an_unauthenticated_provider_on_another_port_is_blocked_with_zero_bytes(
    migrated_engine: AsyncEngine,
) -> None:
    """M2: an allow-list entry carrying a port matches that port exactly."""
    async with FakeProvider() as provider:
        h = Harness(
            dsn(migrated_engine),
            provider,
            product_knowledge_endpoint_allowlist=f"open.test:{provider.port + 1}",
        )

        response = await run(
            h,
            lambda c, csrf: post(
                c, csrf, endpoint_url=f"http://open.test:{provider.port}/mcp", credential_ref=None
            ),
        )

    assert response.json()["status"] == "blocked_by_policy"
    assert h.bytes_sent == 0
    assert h.probe_calls == []


async def test_an_unauthenticated_provider_on_the_listed_port_is_testable(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider() as provider:
        h = Harness(
            dsn(migrated_engine),
            provider,
            product_knowledge_endpoint_allowlist=f"open.test:{provider.port}",
        )

        response = await run(
            h,
            lambda c, csrf: post(
                c, csrf, endpoint_url=f"http://open.test:{provider.port}/mcp", credential_ref=None
            ),
        )

    assert response.json()["status"] == "ok"


async def test_a_credential_pinned_to_another_port_is_422_and_sends_zero_bytes(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=TOKEN) as provider:
        pinned = json.dumps(
            {"wiki": {"token": TOKEN, "allowed_hosts": [f"{HOST}:{provider.port + 1}"]}}
        )
        h = Harness(dsn(migrated_engine), provider, credentials=pinned)

        response = await run(
            h, lambda c, csrf: post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "credential_host_not_bound"
    assert h.bytes_sent == 0


async def test_kill_switch_off_returns_disabled_without_touching_the_provider(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=TOKEN) as provider:
        h = Harness(dsn(migrated_engine), provider, product_knowledge_enabled=False)

        response = await run(
            h, lambda c, csrf: post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert h.probe_calls == []
    assert h.bytes_sent == 0


async def test_a_malformed_credential_map_reports_disabled_not_a_boot_failure(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=TOKEN) as provider:
        h = Harness(
            dsn(migrated_engine),
            provider,
            credentials='{"wiki": {"token": "' + TOKEN + '", "allowed_hosts": []}}',
        )

        response = await run(
            h, lambda c, csrf: post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert TOKEN not in response.text
    assert h.bytes_sent == 0


async def test_a_malformed_endpoint_allowlist_reports_disabled_not_a_boot_failure(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=TOKEN) as provider:
        h = Harness(dsn(migrated_engine), provider, product_knowledge_endpoint_allowlist="*")

        response = await run(
            h, lambda c, csrf: post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert h.bytes_sent == 0


async def test_invalid_request_bodies_are_422(migrated_engine: AsyncEngine) -> None:
    async with FakeProvider() as provider:
        h = Harness(dsn(migrated_engine), provider)

        def drive(c: TestClient, csrf: str) -> list[int]:
            return [
                post(c, csrf, endpoint_url="ftp://wiki.test/mcp").status_code,
                post(c, csrf, endpoint_url="http://u:p@wiki.test/mcp").status_code,
                post(c, csrf, endpoint_url=h.endpoint, credential_ref="Bad Ref").status_code,
                post(c, csrf).status_code,
            ]

        statuses = await run(h, drive)

    assert statuses == [422, 422, 422, 422]
    assert h.bytes_sent == 0
