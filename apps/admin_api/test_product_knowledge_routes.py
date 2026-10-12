"""Integration tests for ``apps/admin_api/product_knowledge_routes.py`` (plan
``plan-deliver-product-knowledge.md`` Phase 6 / 6b; US-PK9, US-PK10; A2),
driven through the real Admin API app against a real Postgres
(``testcontainers-python``) and a real fake MCP provider socket server.

The egress guard and the MCP client are the production code path: the only
substituted piece is the *last hop* (the endpoint host resolves to a private
address and ``LoopbackTransport`` connects to the fake's loopback socket), so
"zero bytes sent" is asserted on the fake's own request log and on the
loopback transport.

The app is driven from a worker thread (``asyncio.to_thread``) so the fake
provider's server keeps running on the test's event loop meanwhile.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.product_knowledge.client import ClientTimeouts, McpProductKnowledgeClient
from adapter.product_knowledge.credentials import (
    CredentialMap,
    CredentialMapError,
    parse_credential_map,
    parse_endpoint_allowlist,
)
from adapter.product_knowledge.factory import ProviderClientFactory
from adapter.product_knowledge.testing.fake_provider import (
    ERROR_BODY_CANARY,
    HOSTILE_TEXT,
    Era,
    FakeProvider,
    Fault,
)
from adapter.product_knowledge.testing.loopback import LoopbackTransport, fixed_resolver
from adapter.product_knowledge.transport import EgressPolicy
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.models import AdminAuditLogRow
from apps.admin_api.dependencies import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from apps.admin_api.main import create_app
from apps.admin_api.product_knowledge_routes import (
    _MESSAGES,
    GLOBAL_LIMIT_PER_MINUTE,
    SESSION_LIMIT_PER_MINUTE,
    ProbeStatus,
)
from apps.admin_api.settings import AdminAPISettings
from review.product_knowledge import ProductKnowledgeProvider

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_USERNAME = "integration-test-admin"
_PASSWORD = "correct-horse-battery-staple"
_TOKEN = "CANARY-PK-ROUTE-TOKEN-2c26b46b68ffc68ff99b453c1d304134"
_HOST = "wiki.test"
_PRIVATE = "10.9.8.7"
_MAP = json.dumps({"wiki": {"token": _TOKEN, "allowed_hosts": [_HOST]}})
_FAST = ClientTimeouts(connect_seconds=1.0, call_seconds=1.0, teardown_grace_seconds=0.5)


class _Harness:
    """One app, one fake provider, and the loopback transports every probe
    built (to assert zero bytes on the wire)."""

    def __init__(self, dsn: str, provider: FakeProvider, **settings: object) -> None:
        self.provider = provider
        self.loopbacks: list[LoopbackTransport] = []
        self.probe_calls: list[tuple[str, str | None]] = []
        credentials_raw = settings.pop("credentials", _MAP)
        resolve_to = str(settings.pop("resolve_to", _PRIVATE))
        deprecated = frozenset[str](settings.pop("deprecated_majors", ()))  # type: ignore[call-overload]
        try:
            credentials = parse_credential_map(credentials_raw)  # type: ignore[arg-type]
        except CredentialMapError:
            credentials = CredentialMap.empty()  # the app under test fails open itself

        try:  # the probe's policy mirrors what apps/admin_api/main.py wires in
            allowlist = parse_endpoint_allowlist(
                str(settings.get("product_knowledge_endpoint_allowlist", ""))
            )
        except CredentialMapError:
            allowlist = ()

        def probe(endpoint_url: str, credential_ref: str | None) -> ProductKnowledgeProvider:
            self.probe_calls.append((endpoint_url, credential_ref))
            loopback = LoopbackTransport(provider.port)
            self.loopbacks.append(loopback)
            if deprecated:
                return McpProductKnowledgeClient(
                    endpoint_url,
                    credential=credentials.get(credential_ref) if credential_ref else None,
                    policy=EgressPolicy(endpoint_allowlist=allowlist),
                    timeouts=_FAST,
                    bypass_cache=True,
                    top_k=1,
                    resolver=fixed_resolver(resolve_to),
                    inner_transport=loopback,
                    deprecated_majors=deprecated,
                )
            return ProviderClientFactory(
                credentials=credentials,
                policy=EgressPolicy(endpoint_allowlist=allowlist),
                timeouts=_FAST,
                bypass_cache=True,
                top_k=1,
                resolver=fixed_resolver(resolve_to),
                inner_transport=loopback,
            )(endpoint_url, credential_ref)

        app_settings = AdminAPISettings(
            environment="development",
            database_url=SecretStr(dsn),
            admin_bootstrap_username=_USERNAME,
            admin_bootstrap_password=SecretStr(_PASSWORD),
            admin_csrf_secret=SecretStr("integration-test-csrf-secret"),
            session_cookie_secure=False,
            product_knowledge_credentials=(
                SecretStr(credentials_raw) if isinstance(credentials_raw, str) else None
            ),
            _env_file=None,
            **settings,  # type: ignore[arg-type]
        )
        self.app = create_app(app_settings, probe_factory=probe)

    @property
    def endpoint(self) -> str:
        return f"http://{_HOST}:{self.provider.port}/mcp"

    @property
    def bytes_sent(self) -> int:
        return len(self.provider.requests)

    @property
    def pinned_hosts(self) -> list[str]:
        return [h for lb in self.loopbacks for h in lb.pinned_hosts]


def _dsn(engine: AsyncEngine) -> str:
    return str(engine.url.render_as_string(hide_password=False))


def _login(client: TestClient) -> str:
    client.post("/api/v1/auth/login", json={"username": _USERNAME, "password": _PASSWORD})
    return str(client.cookies[CSRF_COOKIE_NAME])


def _post(client: TestClient, csrf: str, **body: object) -> Any:
    return client.post(
        "/api/v1/product-knowledge/test-connection",
        json=body,
        headers={CSRF_HEADER_NAME: csrf},
    )


async def _run[T](harness: _Harness, fn: Callable[[TestClient, str], T]) -> T:
    def drive() -> T:
        with TestClient(harness.app) as client:
            return fn(client, _login(client))

    return await asyncio.to_thread(drive)


async def _audit_rows(dsn: str) -> list[AdminAuditLogRow]:
    engine = create_storage_engine(dsn)
    try:
        async with create_session_factory(engine)() as session:
            return list(
                (
                    await session.execute(
                        select(AdminAuditLogRow).where(
                            AdminAuditLogRow.action == "product_knowledge_connection_tested"
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()


# --- US-PK10: the status classes against the fake provider ---------------------


@pytest.mark.parametrize("era", [Era.MODERN, Era.LEGACY])
async def test_a_conforming_provider_is_ok_with_only_validated_fields(
    migrated_engine: AsyncEngine, era: Era
) -> None:
    async with FakeProvider(era=era, token=_TOKEN) as provider:
        h = _Harness(_dsn(migrated_engine), provider)

        response = await _run(
            h, lambda c, csrf: _post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["negotiated_version"] == "1"
    assert body["provider_supported_versions"] == ["1"]
    assert set(body["tools_present"]) <= {
        "product_knowledge_contract",
        "product_knowledge_search",
        "product_knowledge_fetch",
        "product_knowledge_related",
    }
    assert isinstance(body["latency_ms"], int)
    # The probe sent the real token to the bound host only.
    assert {r.authorization for r in provider.requests} == {f"Bearer {_TOKEN}"}
    assert _TOKEN not in response.text
    # The fixed probe query, one result requested.
    searches = [r for r in provider.requests if r.tool == "product_knowledge_search"]
    assert searches and searches[0].arguments is not None
    assert searches[0].arguments["query"] == "connection test"
    assert searches[0].arguments["top_k"] == 1


_STATUS_CASES: list[tuple[str, dict[str, Any], str]] = [
    ("auth_failed", {"token": "a-different-token"}, "auth_failed"),
    ("no_contract", {"omit_tools": frozenset({"product_knowledge_contract"})}, "no_contract"),
    ("unsupported_version", {"supported_versions": ["99"]}, "unsupported_version"),
    (
        "malformed_contract",
        {"fault": Fault("bad_structure", tool="product_knowledge_contract")},
        "malformed",
    ),
    (
        "malformed_search",
        {"fault": Fault("malformed", tool="product_knowledge_search")},
        "malformed",
    ),
    ("provider_5xx", {"fault": Fault("status500", tool="product_knowledge_search")}, "unreachable"),
    ("hang", {"fault": Fault("hang", tool="product_knowledge_search")}, "timeout"),
    (
        "redirect",
        {"fault": Fault("redirect", tool="product_knowledge_search")},
        "blocked_by_policy",
    ),
]


@pytest.mark.parametrize(("name", "provider_kwargs", "expected"), _STATUS_CASES)
async def test_each_provider_failure_class_maps_to_its_status_with_no_extra_fields(
    migrated_engine: AsyncEngine, name: str, provider_kwargs: dict[str, Any], expected: str
) -> None:
    kwargs = {"token": _TOKEN, **provider_kwargs}
    async with FakeProvider(**kwargs) as provider:
        h = _Harness(_dsn(migrated_engine), provider)

        response = await _run(
            h, lambda c, csrf: _post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.status_code == 200, name
    body = response.json()
    assert body["status"] == expected, name
    assert body["status"] in get_args(ProbeStatus), name
    # Only a conforming response echoes protocol details or timing.
    for field in (
        "negotiated_version",
        "provider_supported_versions",
        "tools_present",
        "latency_ms",
    ):
        assert body[field] is None, (name, field)


async def test_a_non_conforming_supported_versions_entry_is_malformed_and_never_echoed(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=_TOKEN, supported_versions=["1", "<script>"]) as provider:
        h = _Harness(_dsn(migrated_engine), provider)

        response = await _run(
            h, lambda c, csrf: _post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "malformed"
    assert "<script>" not in response.text
    assert body["provider_supported_versions"] is None
    assert body["negotiated_version"] is None


async def test_a_deprecated_major_is_reported_as_deprecated_version_within_the_closed_set(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=_TOKEN) as provider:
        h = _Harness(_dsn(migrated_engine), provider, deprecated_majors={"1"})

        response = await _run(
            h, lambda c, csrf: _post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "deprecated_version"
    assert body["negotiated_version"] == "1"  # still works: fields are reported
    # Closed set: every status the API can emit has a review-generated message.
    assert set(get_args(ProbeStatus)) == set(_MESSAGES)


async def test_provider_text_is_never_echoed_in_the_message(migrated_engine: AsyncEngine) -> None:
    async with FakeProvider(
        token=_TOKEN, fault=Fault("error_result", tool="product_knowledge_search")
    ) as provider:
        h = _Harness(_dsn(migrated_engine), provider)
        response = await _run(
            h, lambda c, csrf: _post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )
    async with FakeProvider(token=_TOKEN, fault=Fault("hostile_metadata")) as hostile:
        h2 = _Harness(_dsn(migrated_engine), hostile)
        response2 = await _run(
            h2, lambda c, csrf: _post(c, csrf, endpoint_url=h2.endpoint, credential_ref="wiki")
        )

    for r in (response, response2):
        assert r.status_code == 200
        assert ERROR_BODY_CANARY not in r.text
        assert HOSTILE_TEXT not in r.text
        assert _TOKEN not in r.text


async def test_a_loopback_resolution_is_refused_by_the_egress_guard_before_any_bytes(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=_TOKEN) as provider:
        h = _Harness(_dsn(migrated_engine), provider, resolve_to="127.0.0.1")

        response = await _run(
            h, lambda c, csrf: _post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.json()["status"] == "blocked_by_policy"
    assert response.json()["latency_ms"] is None
    assert h.bytes_sent == 0
    assert h.pinned_hosts == []


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
    async with FakeProvider(token=_TOKEN) as provider:
        h = _Harness(_dsn(migrated_engine), provider)
        payload: dict[str, str | None] = {"endpoint_url": h.endpoint, "credential_ref": "wiki"}
        payload.update({k: v.format(port=provider.port) for k, v in body.items()})

        response = await _run(h, lambda c, csrf: _post(c, csrf, **payload))

    assert response.status_code == 422
    assert response.json()["detail"] == code
    assert h.probe_calls == []  # the adapter was never even constructed
    assert h.bytes_sent == 0
    assert h.pinned_hosts == []


async def test_an_unprovisioned_target_gets_the_collapsed_status_and_no_timing(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider() as provider:
        h = _Harness(_dsn(migrated_engine), provider)

        response = await _run(
            h,
            lambda c, csrf: _post(
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
        h = _Harness(
            _dsn(migrated_engine),
            provider,
            product_knowledge_endpoint_allowlist="open.test",
        )

        response = await _run(
            h,
            lambda c, csrf: _post(
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
        h = _Harness(
            _dsn(migrated_engine),
            provider,
            product_knowledge_endpoint_allowlist=f"open.test:{provider.port + 1}",
        )

        response = await _run(
            h,
            lambda c, csrf: _post(
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
        h = _Harness(
            _dsn(migrated_engine),
            provider,
            product_knowledge_endpoint_allowlist=f"open.test:{provider.port}",
        )

        response = await _run(
            h,
            lambda c, csrf: _post(
                c, csrf, endpoint_url=f"http://open.test:{provider.port}/mcp", credential_ref=None
            ),
        )

    assert response.json()["status"] == "ok"


async def test_a_credential_pinned_to_another_port_is_422_and_sends_zero_bytes(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=_TOKEN) as provider:
        pinned = json.dumps(
            {"wiki": {"token": _TOKEN, "allowed_hosts": [f"{_HOST}:{provider.port + 1}"]}}
        )
        h = _Harness(_dsn(migrated_engine), provider, credentials=pinned)

        response = await _run(
            h, lambda c, csrf: _post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "credential_host_not_bound"
    assert h.bytes_sent == 0


async def test_kill_switch_off_returns_disabled_without_touching_the_provider(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=_TOKEN) as provider:
        h = _Harness(_dsn(migrated_engine), provider, product_knowledge_enabled=False)

        response = await _run(
            h, lambda c, csrf: _post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert h.probe_calls == []
    assert h.bytes_sent == 0


async def test_a_malformed_credential_map_reports_disabled_not_a_boot_failure(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=_TOKEN) as provider:
        h = _Harness(
            _dsn(migrated_engine),
            provider,
            credentials='{"wiki": {"token": "' + _TOKEN + '", "allowed_hosts": []}}',
        )

        response = await _run(
            h, lambda c, csrf: _post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert _TOKEN not in response.text
    assert h.bytes_sent == 0


async def test_a_malformed_endpoint_allowlist_reports_disabled_not_a_boot_failure(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=_TOKEN) as provider:
        h = _Harness(_dsn(migrated_engine), provider, product_knowledge_endpoint_allowlist="*")

        response = await _run(
            h, lambda c, csrf: _post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
        )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert h.bytes_sent == 0


async def test_invalid_request_bodies_are_422(migrated_engine: AsyncEngine) -> None:
    async with FakeProvider() as provider:
        h = _Harness(_dsn(migrated_engine), provider)

        def drive(c: TestClient, csrf: str) -> list[int]:
            return [
                _post(c, csrf, endpoint_url="ftp://wiki.test/mcp").status_code,
                _post(c, csrf, endpoint_url="http://u:p@wiki.test/mcp").status_code,
                _post(c, csrf, endpoint_url=h.endpoint, credential_ref="Bad Ref").status_code,
                _post(c, csrf).status_code,
            ]

        statuses = await _run(h, drive)

    assert statuses == [422, 422, 422, 422]
    assert h.bytes_sent == 0


# --- audit -----------------------------------------------------------------------


async def test_every_probe_is_audited_with_actor_host_and_status_never_a_token(
    migrated_engine: AsyncEngine,
) -> None:
    dsn = _dsn(migrated_engine)
    async with FakeProvider(token=_TOKEN) as provider:
        h = _Harness(dsn, provider)

        def drive(c: TestClient, csrf: str) -> None:
            _post(c, csrf, endpoint_url=h.endpoint, credential_ref="wiki")
            _post(c, csrf, endpoint_url=f"http://scan-me.internal:{provider.port}/mcp")
            _post(c, csrf, endpoint_url=h.endpoint, credential_ref="nope")

        await _run(h, drive)

    rows = sorted(await _audit_rows(dsn), key=lambda r: r.created_at)
    assert [r.extra["status"] for r in rows] == [
        "ok",
        "blocked_by_policy",
        "unknown_credential_ref",
    ]
    assert [r.extra["endpoint_host"] for r in rows] == [_HOST, "scan-me.internal", _HOST]
    assert rows[0].extra["credential_ref"] == "wiki"
    assert all(r.actor_user_id is not None for r in rows)
    assert _TOKEN not in str([r.extra for r in rows])


# --- rate limits -----------------------------------------------------------------


async def test_session_limit_returns_429_after_six_calls_a_minute(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider() as provider:
        h = _Harness(_dsn(migrated_engine), provider)

        def drive(c: TestClient, csrf: str) -> list[int]:
            return [
                _post(c, csrf, endpoint_url=f"http://scan.internal:{provider.port}/mcp").status_code
                for _ in range(SESSION_LIMIT_PER_MINUTE + 1)
            ]

        statuses = await _run(h, drive)

    assert statuses == [200] * SESSION_LIMIT_PER_MINUTE + [429]
    assert h.bytes_sent == 0


async def test_global_limit_returns_429_across_users(migrated_engine: AsyncEngine) -> None:
    async with FakeProvider() as provider:
        h = _Harness(_dsn(migrated_engine), provider)
        users_needed = GLOBAL_LIMIT_PER_MINUTE // SESSION_LIMIT_PER_MINUTE

        def drive() -> tuple[list[int], int]:
            # Distinct users, so neither the per-session nor the per-user
            # limit trips: only the global window can produce the 429.
            names = [f"pkuser{i}" for i in range(users_needed + 1)]
            ok: list[int] = []
            with TestClient(h.app) as client:
                csrf = _login(client)
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
                        _post(
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
        h = _Harness(_dsn(migrated_engine), provider)

        def drive() -> tuple[list[int], int]:
            ok: list[int] = []
            with TestClient(h.app) as client:
                for _ in range(2):  # two sessions, same user
                    csrf = _login(client)
                    ok.extend(
                        _post(
                            client, csrf, endpoint_url=f"http://scan.internal:{provider.port}/mcp"
                        ).status_code
                        for _ in range(SESSION_LIMIT_PER_MINUTE // 2)
                    )
                csrf = _login(client)  # third session, user already at its limit
                over = _post(
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
    dsn = _dsn(migrated_engine)
    async with FakeProvider() as provider:
        h = _Harness(dsn, provider)
        admin_body = await _run(h, lambda c, csrf: c.get("/api/v1/product-knowledge/credentials"))

    assert admin_body.status_code == 200
    assert admin_body.json() == [{"ref": "wiki", "allowed_hosts": [_HOST]}]
    assert _TOKEN not in admin_body.text


async def test_credentials_list_requires_a_session(migrated_engine: AsyncEngine) -> None:
    async with FakeProvider() as provider:
        h = _Harness(_dsn(migrated_engine), provider)

        def drive() -> int:
            with TestClient(h.app) as client:
                return int(client.get("/api/v1/product-knowledge/credentials").status_code)

        status = await asyncio.to_thread(drive)

    assert status == 401


async def test_test_connection_requires_a_session_and_a_csrf_token(
    migrated_engine: AsyncEngine,
) -> None:
    async with FakeProvider(token=_TOKEN) as provider:
        h = _Harness(_dsn(migrated_engine), provider)
        body = {"endpoint_url": h.endpoint, "credential_ref": "wiki"}

        def drive() -> tuple[int, int]:
            with TestClient(h.app) as client:
                anonymous = client.post("/api/v1/product-knowledge/test-connection", json=body)
                _login(client)  # session cookie set, CSRF header deliberately omitted
                no_csrf = client.post("/api/v1/product-knowledge/test-connection", json=body)
            return anonymous.status_code, no_csrf.status_code

        statuses = await asyncio.to_thread(drive)

    assert statuses == (401, 403)
    assert h.probe_calls == []
    assert h.bytes_sent == 0
