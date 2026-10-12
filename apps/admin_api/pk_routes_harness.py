"""
Shared harness for the product-knowledge test-connection integration tests.

Used by ``test_product_knowledge_routes*.py``: one real Admin API app, one
fake MCP provider and the loopback transports every probe built.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

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
    FakeProvider,
)
from adapter.product_knowledge.testing.loopback import LoopbackTransport, fixed_resolver
from adapter.product_knowledge.transport import EgressPolicy
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.models import AdminAuditLogRow
from apps.admin_api.dependencies import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from apps.admin_api.main import create_app
from apps.admin_api.settings import AdminAPISettings
from review.product_knowledge import ProductKnowledgeProvider

USERNAME = "integration-test-admin"
PASSWORD = "correct-horse-battery-staple"
TOKEN = "CANARY-PK-ROUTE-TOKEN-2c26b46b68ffc68ff99b453c1d304134"
HOST = "wiki.test"
PRIVATE = "10.9.8.7"
MAP = json.dumps({"wiki": {"token": TOKEN, "allowed_hosts": [HOST]}})
FAST = ClientTimeouts(connect_seconds=1.0, call_seconds=1.0, teardown_grace_seconds=0.5)


class Harness:
    """
    One app, one fake provider, and the loopback transports every probe built.

    The transports let tests assert zero bytes on the wire.
    """

    def __init__(self, dsn: str, provider: FakeProvider, **settings: object) -> None:
        """Build the app, fake provider and loopback transports."""
        self.provider = provider
        self.loopbacks: list[LoopbackTransport] = []
        self.probe_calls: list[tuple[str, str | None]] = []
        credentials_raw = settings.pop("credentials", MAP)
        resolve_to = str(settings.pop("resolve_to", PRIVATE))
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
                    timeouts=FAST,
                    bypass_cache=True,
                    top_k=1,
                    resolver=fixed_resolver(resolve_to),
                    inner_transport=loopback,
                    deprecated_majors=deprecated,
                )
            return ProviderClientFactory(
                credentials=credentials,
                policy=EgressPolicy(endpoint_allowlist=allowlist),
                timeouts=FAST,
                bypass_cache=True,
                top_k=1,
                resolver=fixed_resolver(resolve_to),
                inner_transport=loopback,
            )(endpoint_url, credential_ref)

        app_settings = AdminAPISettings(
            environment="development",
            database_url=SecretStr(dsn),
            admin_bootstrap_username=USERNAME,
            admin_bootstrap_password=SecretStr(PASSWORD),
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
        return f"http://{HOST}:{self.provider.port}/mcp"

    @property
    def bytes_sent(self) -> int:
        return len(self.provider.requests)

    @property
    def pinned_hosts(self) -> list[str]:
        return [h for lb in self.loopbacks for h in lb.pinned_hosts]


def dsn(engine: AsyncEngine) -> str:
    return str(engine.url.render_as_string(hide_password=False))


def login(client: TestClient) -> str:
    client.post("/api/v1/auth/login", json={"username": USERNAME, "password": PASSWORD})
    return str(client.cookies[CSRF_COOKIE_NAME])


def post(client: TestClient, csrf: str, **body: object) -> Any:
    return client.post(
        "/api/v1/product-knowledge/test-connection",
        json=body,
        headers={CSRF_HEADER_NAME: csrf},
    )


async def run[T](harness: Harness, fn: Callable[[TestClient, str], T]) -> T:
    def drive() -> T:
        with TestClient(harness.app) as client:
            return fn(client, login(client))

    return await asyncio.to_thread(drive)


async def audit_rows(dsn: str) -> list[AdminAuditLogRow]:
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
