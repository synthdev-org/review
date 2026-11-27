"""
Shared harness for the classifier Admin API integration tests.

One real Admin API app against real Postgres, a credential map with a canary token, and a
classifier factory built on the real ``TypeSafeClassifier`` whose wire is faked one layer
down (``httpx2.MockTransport`` behind the egress guard, which still runs).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx2
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter.classifier.typesafe import CircuitBreaker, ClassifierSeams, TypeSafeClassifier
from adapter.egress.credentials import parse_credential_map
from adapter.egress.transport import EgressPolicy
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.models import AdminAuditLogRow, Org
from apps.admin_api.dependencies import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from apps.admin_api.main import create_app
from apps.admin_api.settings import AdminAPISettings
from llmgateway.classifier import Classifier

USERNAME = "integration-test-admin"
PASSWORD = "correct-horse-battery-staple"
TOKEN = "CANARY-CLASSIFIER-TOKEN-7f3c9a1e5b2d4c60"
HOST = "api.typesafe.ai"
MAP = json.dumps(
    {
        "typesafe": {"token": TOKEN, "allowed_hosts": [HOST]},
        "other": {"token": TOKEN + "-2", "allowed_hosts": ["other.example.com"]},
    }
)
PUBLIC_IP = "93.184.216.34"
PROBE_ORG_ID = "00000000-0000-4000-8000-0000000000a1"  # seeded by ``build_app``
NOUL_BODY = {
    "model": "jev-1.14.0",
    "answers": {"probe": {"type": "noul", "noul": 0.97}},
    "usage": {"input_tokens": 10, "output_tokens": 2},
}


def value(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "enabled": True,
        "backend": "typesafe",
        "endpoint_url": f"https://{HOST}",
        "credential_ref": "typesafe",
        "model": "jev-latest",
        "mode": "shadow",
        "disclosure_acknowledged": True,
    }
    return base | overrides


def build_app(
    migrated_engine: AsyncEngine,
    handler: Callable[[httpx2.Request], httpx2.Response] | None = None,
    *,
    resolve_to: str = PUBLIC_IP,
) -> Any:
    credentials = parse_credential_map(MAP)
    asyncio.run(_seed_probe_org(migrated_engine))

    async def resolver(host: str, port: int) -> list[str]:
        return [resolve_to]

    def factory(endpoint_url: str, ref: str) -> Classifier:
        assert handler is not None, "no probe expected"
        return TypeSafeClassifier(
            endpoint_url,
            credential=credentials.get(ref),  # type: ignore[arg-type]
            policy=EgressPolicy(allow_private_networks=False),
            breaker=CircuitBreaker(),
            seams=ClassifierSeams(resolver=resolver, inner_transport=httpx2.MockTransport(handler)),
        )

    settings = AdminAPISettings(
        environment="development",
        database_url=SecretStr(_dsn(migrated_engine)),
        admin_bootstrap_username=USERNAME,
        admin_bootstrap_password=SecretStr(PASSWORD),
        admin_csrf_secret=SecretStr("integration-test-csrf-secret"),
        session_cookie_secure=False,
        product_knowledge_credentials=SecretStr(MAP),
        _env_file=None,
    )
    return create_app(settings, classifier_factory=factory)


def login(client: TestClient) -> str:
    client.post("/api/v1/auth/login", json={"username": USERNAME, "password": PASSWORD})
    return str(client.cookies[CSRF_COOKIE_NAME])


def put(client: TestClient, csrf: str, org_id: object, **overrides: object) -> Any:
    return client.put(
        f"/api/v1/config/org:{org_id}/classifier_provider",
        json={"value": value(**overrides)},
        headers={CSRF_HEADER_NAME: csrf},
    )


def probe(client: TestClient, csrf: str, org_id: object = PROBE_ORG_ID, **overrides: object) -> Any:
    body: dict[str, object] = {
        "org_id": str(org_id),
        "endpoint_url": f"https://{HOST}",
        "credential_ref": "typesafe",
        "model": "jev-latest",
    }
    return client.post(
        "/api/v1/classifier/test-connection",
        json=body | overrides,
        headers={CSRF_HEADER_NAME: csrf},
    )


def _dsn(engine: AsyncEngine) -> str:
    return str(engine.url.render_as_string(hide_password=False))


async def _seed_probe_org(migrated_engine: AsyncEngine) -> None:
    engine = create_storage_engine(_dsn(migrated_engine))
    try:
        async with create_session_factory(engine)() as session, session.begin():
            await session.merge(Org(id=uuid.UUID(PROBE_ORG_ID), name="probe", slug="probe-org"))
    finally:
        await engine.dispose()


async def seed_orgs(migrated_engine: AsyncEngine, count: int = 1) -> list[uuid.UUID]:
    engine = create_storage_engine(_dsn(migrated_engine))  # own engine: this runs on its own loop
    try:
        async with create_session_factory(engine)() as session, session.begin():
            orgs = [Org(name=f"acme{i}", slug=f"acme-{uuid.uuid4().hex}") for i in range(count)]
            session.add_all(orgs)
            await session.flush()
            return [o.id for o in orgs]
    finally:
        await engine.dispose()


async def audit_rows(migrated_engine: AsyncEngine, action: str) -> list[AdminAuditLogRow]:
    engine = create_storage_engine(_dsn(migrated_engine))
    try:
        async with create_session_factory(engine)() as session:
            result = await session.execute(
                select(AdminAuditLogRow).where(AdminAuditLogRow.action == action)
            )
            return list(result.scalars().all())
    finally:
        await engine.dispose()
