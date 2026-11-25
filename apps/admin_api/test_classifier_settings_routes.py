"""
Integration tests for the ``classifier_provider`` setting (plan Phase 8; US-C1, US-C2).

Real Admin API app against real Postgres (testcontainers).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.admin_api.classifier_harness import (
    HOST,
    TOKEN,
    audit_rows,
    build_app,
    login,
    put,
    seed_orgs,
    value,
)
from apps.admin_api.dependencies import CSRF_HEADER_NAME

pytestmark = pytest.mark.integration


def _orgs(engine: AsyncEngine, count: int = 1) -> list[uuid.UUID]:
    return asyncio.run(seed_orgs(engine, count))


def test_an_acknowledged_write_is_stored_without_a_token_and_audited_by_host_ref_enabled_mode(
    migrated_engine: AsyncEngine,
) -> None:
    (org,) = _orgs(migrated_engine)
    with TestClient(build_app(migrated_engine)) as client:
        csrf = login(client)
        response = put(client, csrf, org)
        tree = client.get("/api/v1/config").text

    assert response.status_code == 200
    assert TOKEN not in response.text + tree
    row = next(r for r in asyncio.run(audit_rows(migrated_engine, "config_setting_updated")))
    assert row.extra["new"] == {
        "endpoint_host": HOST,
        "credential_ref": "typesafe",
        "enabled": True,
        "mode": "shadow",
    }
    assert row.extra["old"] is None
    assert TOKEN not in str(row.extra)


def test_enabling_without_the_acknowledgement_is_refused_by_the_api_and_stores_nothing(
    migrated_engine: AsyncEngine,
) -> None:
    (org,) = _orgs(migrated_engine)
    with TestClient(build_app(migrated_engine)) as client:
        csrf = login(client)
        refused = put(client, csrf, org, disclosure_acknowledged=False)
        omitted = client.put(
            f"/api/v1/config/org:{org}/classifier_provider",
            json={
                "value": {
                    "enabled": True,
                    "backend": "typesafe",
                    "endpoint_url": f"https://{HOST}",
                    "credential_ref": "typesafe",
                    "model": "jev-latest",
                    "mode": "shadow",
                }
            },
            headers={CSRF_HEADER_NAME: csrf},
        )
        disabled = put(client, csrf, org, enabled=False, disclosure_acknowledged=False)

    assert refused.status_code == omitted.status_code == 422
    assert refused.json()["detail"] == "disclosure_not_acknowledged"
    assert disabled.status_code == 200  # opting out never needs an acknowledgement


@pytest.mark.parametrize("scope_kind", ["platform", "repo", "team"])
def test_every_scope_but_org_is_rejected(migrated_engine: AsyncEngine, scope_kind: str) -> None:
    (org,) = _orgs(migrated_engine)
    scope = {
        "platform": "platform",
        "repo": f"repo:{org}:{uuid.uuid4()}",
        "team": f"team:{org}:{uuid.uuid4()}",
    }[scope_kind]
    with TestClient(build_app(migrated_engine)) as client:
        csrf = login(client)
        response = client.put(
            f"/api/v1/config/{scope}/classifier_provider",
            json={"value": value()},
            headers={CSRF_HEADER_NAME: csrf},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "org_scope_only"


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"credential_ref": "nope"}, "unknown_credential_ref"),
        ({"credential_ref": "other"}, "credential_host_not_bound"),
        ({"endpoint_url": "https://evil.example.org"}, "credential_host_not_bound"),
        ({"endpoint_url": f"https://{HOST}.evil.com"}, "credential_host_not_bound"),
        ({"endpoint_url": "https://127.0.0.1"}, "endpoint_host_denied"),
        ({"endpoint_url": "https://169.254.169.254"}, "endpoint_host_denied"),
        ({"endpoint_url": "https://10.0.0.5"}, "endpoint_host_denied"),
        ({"endpoint_url": "https://localhost"}, "endpoint_host_denied"),
        ({"endpoint_url": "https://[::1]"}, "endpoint_host_denied"),
    ],
)
def test_denied_hosts_and_credential_mismatches_are_specific_422s(
    migrated_engine: AsyncEngine, overrides: dict[str, object], detail: str
) -> None:
    (org,) = _orgs(migrated_engine)
    with TestClient(build_app(migrated_engine)) as client:
        response = put(client, login(client), org, **overrides)

    assert response.status_code == 422
    assert response.json()["detail"] == detail


@pytest.mark.parametrize(
    "overrides",
    [
        {"backend": "openai"},
        {"endpoint_url": f"http://{HOST}"},
        {"endpoint_url": f"https://u:{TOKEN}@{HOST}"},
        {"mode": "off"},
    ],
)
def test_invalid_shapes_are_422_and_never_echo_the_offered_url(
    migrated_engine: AsyncEngine, overrides: dict[str, object]
) -> None:
    (org,) = _orgs(migrated_engine)
    with TestClient(build_app(migrated_engine)) as client:
        response = put(client, login(client), org, **overrides)

    assert response.status_code == 422
    assert TOKEN not in response.text


def test_two_orgs_keep_their_own_settings(migrated_engine: AsyncEngine) -> None:
    first, second = _orgs(migrated_engine, 2)
    with TestClient(build_app(migrated_engine)) as client:
        csrf = login(client)
        assert put(client, csrf, first, mode="active").status_code == 200
        assert put(client, csrf, second, enabled=False).status_code == 200
        orgs = {o["id"]: o for o in client.get("/api/v1/config").json()["orgs"]}

    assert orgs[str(first)]["effective"]["classifier_provider"]["value"]["mode"] == "active"
    assert orgs[str(second)]["effective"]["classifier_provider"]["value"]["enabled"] is False
