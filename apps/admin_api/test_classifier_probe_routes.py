"""
Integration tests for ``POST /api/v1/classifier/test-connection`` (plan Phase 8; US-C3).

The real Admin API app, Postgres, and the real ``TypeSafeClassifier`` over the egress
guard; only the wire below the guard is faked.
"""

from __future__ import annotations

import asyncio
import json

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.admin_api.classifier_harness import (
    NOUL_BODY,
    TOKEN,
    audit_rows,
    build_app,
    login,
    probe,
)
from apps.admin_api.dependencies import CSRF_HEADER_NAME
from apps.admin_api.probe_rate_limit import SESSION_LIMIT_PER_MINUTE

pytestmark = pytest.mark.integration


def _answer(status: int, body: object | None = None, **headers: str) -> httpx2.Response:
    return httpx2.Response(status, json=NOUL_BODY if body is None else body, headers=headers)


def _run(
    engine: AsyncEngine, response: httpx2.Response, **overrides: object
) -> tuple[dict[str, object], list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return response

    with TestClient(build_app(engine, handler)) as client:
        reply = probe(client, login(client), **overrides)
    assert reply.status_code == 200, reply.text
    return reply.json(), seen


def test_a_conforming_provider_is_ok_and_sends_one_fixed_synthetic_question(
    migrated_engine: AsyncEngine,
) -> None:
    body, seen = _run(migrated_engine, _answer(200))

    assert body["status"] == "ok"
    assert body["model"] == "jev-latest"
    assert isinstance(body["latency_ms"], int)
    (request,) = seen
    sent = json.loads(request.content)
    assert sent["state"] == "The sky is blue."
    assert list(sent["questions"]) == ["probe"]
    assert request.headers["authorization"] == f"Bearer {TOKEN}"  # the guard attached it


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (_answer(401, {"error": "bad key"}), "unauthorized"),
        (_answer(403, {"error": "bad key"}), "unauthorized"),
        (_answer(500, {"error": "boom"}), "unreachable"),
        (_answer(200, {"not": "noul"}), "malformed"),
        (_answer(429, {"error": "slow"}, **{"retry-after": "120"}), "rate_limited"),
        (_answer(302, {}, location="http://169.254.169.254/"), "unreachable"),
    ],
)
def test_closed_set_failures_never_echo_the_key_or_provider_text(
    migrated_engine: AsyncEngine, response: httpx2.Response, status: str
) -> None:
    body, _ = _run(migrated_engine, response)

    assert body["status"] == status
    assert body["model"] is None
    assert TOKEN not in json.dumps(body)
    assert "bad key" not in json.dumps(body)
    rows = asyncio.run(audit_rows(migrated_engine, "classifier_connection_tested"))
    assert [r.extra["status"] for r in rows] == [status]
    assert TOKEN not in str(rows[0].extra)


def test_dns_rebinding_to_a_private_address_sends_nothing(migrated_engine: AsyncEngine) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return _answer(200)

    with TestClient(build_app(migrated_engine, handler, resolve_to="10.9.8.7")) as client:
        reply = probe(client, login(client))

    assert reply.json()["status"] == "unreachable"
    assert seen == []


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"credential_ref": "other"}, "credential_host_not_bound"),
        ({"credential_ref": "nope"}, "unknown_credential_ref"),
        ({"endpoint_url": "https://127.0.0.1"}, "endpoint_host_denied"),
    ],
)
def test_an_unbound_or_denied_target_is_422_and_zero_bytes_are_sent(
    migrated_engine: AsyncEngine, overrides: dict[str, object], detail: str
) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return _answer(200)

    with TestClient(build_app(migrated_engine, handler)) as client:
        reply = probe(client, login(client), **overrides)

    assert (reply.status_code, reply.json()["detail"]) == (422, detail)
    assert seen == []


def test_the_probe_shares_one_limiter_with_the_product_knowledge_route(
    migrated_engine: AsyncEngine,
) -> None:
    with TestClient(build_app(migrated_engine, lambda r: _answer(200))) as client:
        csrf = login(client)
        half = SESSION_LIMIT_PER_MINUTE // 2
        for _ in range(half):
            client.post(
                "/api/v1/product-knowledge/test-connection",
                json={"endpoint_url": "https://wiki.example.com/mcp"},
                headers={CSRF_HEADER_NAME: csrf},
            )
        statuses = [probe(client, csrf).status_code for _ in range(half + 1)]

    assert statuses == [200] * half + [429]


def test_an_unauthenticated_caller_cannot_probe(migrated_engine: AsyncEngine) -> None:
    with TestClient(build_app(migrated_engine, lambda r: _answer(200))) as client:
        assert probe(client, "no-csrf").status_code in (401, 403)


@pytest.mark.parametrize("org_id", ["org-1", "00000000-0000-4000-8000-0000000000ff"])
def test_an_unknown_org_is_refused_before_any_wire_call_or_limiter_slot(
    migrated_engine: AsyncEngine, org_id: str
) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return _answer(200)

    with TestClient(build_app(migrated_engine, handler)) as client:
        csrf = login(client)
        refused = [probe(client, csrf, org_id=org_id) for _ in range(SESSION_LIMIT_PER_MINUTE + 1)]
        assert {(r.status_code, r.json()["detail"]) for r in refused} == {(422, "unknown_org")}
        assert probe(client, csrf).status_code == 200  # the caller's budget is untouched
    assert len(seen) == 1


def test_invalid_targets_leave_the_probe_quota_untouched(migrated_engine: AsyncEngine) -> None:
    with TestClient(build_app(migrated_engine, lambda r: _answer(200))) as client:
        csrf = login(client)
        bad = [
            probe(client, csrf, endpoint_url="https://127.0.0.1")
            for _ in range(SESSION_LIMIT_PER_MINUTE + 1)
        ]
        assert {r.status_code for r in bad} == {422}  # never 429
        assert probe(client, csrf).status_code == 200  # the budget is untouched
