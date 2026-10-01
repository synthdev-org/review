"""Integration tests for apps/admin_api/usage_routes.py — GET /api/v1/usage
(Unit H, US-A18), driven through the real apps/admin_api FastAPI app via
TestClient, against a real Postgres via testcontainers-python, per
docs/plans/plan-deliver-admin-gui.md's Phase 7 "Tests" section:

"seeded counts above/below 20 render correctly, with no other field
altered by crossing the threshold."
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from adapter.storage.engine import create_session_factory
from adapter.storage.models import Org, PrivateRepoReviewCount, Repo
from adapter.storage.tenancy import day_for
from apps.admin_api.main import create_app
from apps.admin_api.settings import AdminAPISettings
from tenancy.usage import FREE_TIER_DAILY_REVIEW_LIMIT

pytestmark = pytest.mark.integration

_BOOTSTRAP_USERNAME = "integration-test-admin"
_BOOTSTRAP_PASSWORD = "correct-horse-battery-staple"


def _settings(*, dsn: str, **overrides: object) -> AdminAPISettings:
    defaults: dict[str, object] = {
        "environment": "development",
        "database_url": SecretStr(dsn),
        "admin_bootstrap_username": _BOOTSTRAP_USERNAME,
        "admin_bootstrap_password": SecretStr(_BOOTSTRAP_PASSWORD),
        "admin_csrf_secret": SecretStr("integration-test-csrf-secret"),
        "session_cookie_secure": False,
        "_env_file": None,
    }
    defaults.update(overrides)
    return AdminAPISettings(**defaults)  # type: ignore[arg-type]


def _dsn(migrated_engine: AsyncEngine) -> str:
    return str(migrated_engine.url.render_as_string(hide_password=False))


def _login(client: TestClient) -> None:
    client.post(
        "/api/v1/auth/login",
        json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
    )


def _session_factory(engine: AsyncEngine) -> async_sessionmaker[Any]:
    return create_session_factory(engine)


async def _seed_count(
    engine: AsyncEngine, *, installation_id: int, count: int, day: Any | None = None
) -> None:
    resolved_day = day if day is not None else day_for(datetime.now(UTC))
    session_factory = _session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(
            PrivateRepoReviewCount(installation_id=installation_id, day=resolved_day, count=count)
        )


async def _seed_installation_org(
    engine: AsyncEngine, *, installation_id: int, org_name: str
) -> None:
    session_factory = _session_factory(engine)
    async with session_factory() as session, session.begin():
        org = Org(name=org_name, slug=f"{org_name}-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        session.add(
            Repo(
                org_id=org.id,
                name="widgets",
                full_name=f"{org_name}/widgets",
                installation_id=installation_id,
            )
        )


# --- US-A18: GET /api/v1/usage ----------------------------------------------


@pytest.mark.asyncio
async def test_get_usage_marks_over_and_under_threshold_rows_correctly(
    migrated_engine: AsyncEngine,
) -> None:
    over_installation_id = 111111
    under_installation_id = 222222
    await _seed_installation_org(
        migrated_engine, installation_id=over_installation_id, org_name="over-org"
    )
    await _seed_installation_org(
        migrated_engine, installation_id=under_installation_id, org_name="under-org"
    )
    await _seed_count(
        migrated_engine,
        installation_id=over_installation_id,
        count=FREE_TIER_DAILY_REVIEW_LIMIT + 5,
    )
    await _seed_count(
        migrated_engine,
        installation_id=under_installation_id,
        count=FREE_TIER_DAILY_REVIEW_LIMIT - 5,
    )

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login(client)
        response = client.get("/api/v1/usage")

    assert response.status_code == 200
    body = response.json()
    assert body["threshold"] == FREE_TIER_DAILY_REVIEW_LIMIT

    by_installation = {row["installation_id"]: row for row in body["orgs"]}

    over_row = by_installation[over_installation_id]
    assert over_row["count"] == FREE_TIER_DAILY_REVIEW_LIMIT + 5
    assert over_row["over_threshold"] is True
    assert over_row["org_name"] == "over-org"

    under_row = by_installation[under_installation_id]
    assert under_row["count"] == FREE_TIER_DAILY_REVIEW_LIMIT - 5
    assert under_row["over_threshold"] is False
    assert under_row["org_name"] == "under-org"

    # "no other field altered by crossing the threshold" — the only
    # difference between the two rows' own shape is `count`/`over_threshold`
    # /`org_name`; everything else (the response envelope's own keys) is
    # identical in structure between the two.
    assert set(over_row.keys()) == set(under_row.keys())


@pytest.mark.asyncio
async def test_get_usage_at_exactly_the_threshold_is_not_over(migrated_engine: AsyncEngine) -> None:
    """US-A18's own wording is "exceeds 20" — a count of exactly 20 is
    still within the free tier, not over it."""
    installation_id = 333333
    await _seed_count(
        migrated_engine, installation_id=installation_id, count=FREE_TIER_DAILY_REVIEW_LIMIT
    )

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login(client)
        response = client.get("/api/v1/usage")

    body = response.json()
    row = next(row for row in body["orgs"] if row["installation_id"] == installation_id)
    assert row["count"] == FREE_TIER_DAILY_REVIEW_LIMIT
    assert row["over_threshold"] is False


@pytest.mark.asyncio
async def test_get_usage_resolves_installation_with_no_matching_repo_as_orgless(
    migrated_engine: AsyncEngine,
) -> None:
    """A counter row can exist for an installation with no (or no longer
    any) matching `Repo` row — the read degrades to `org_id`/`org_name`
    both `None` rather than excluding the row, per
    `tenancy.usage.UsageCountRow`'s own docstring."""
    installation_id = 444444
    await _seed_count(migrated_engine, installation_id=installation_id, count=1)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login(client)
        response = client.get("/api/v1/usage")

    body = response.json()
    row = next(row for row in body["orgs"] if row["installation_id"] == installation_id)
    assert row["org_id"] is None
    assert row["org_name"] is None
    assert row["count"] == 1


def test_get_usage_with_no_reviews_today_returns_an_empty_list(
    migrated_engine: AsyncEngine,
) -> None:
    """US-A18's first Given/When/Then is scoped to "at least one org has
    performed private-repo reviews today" — this is the complementary
    empty-state case: no counter rows at all yet."""
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login(client)
        response = client.get("/api/v1/usage")

    assert response.status_code == 200
    assert response.json()["orgs"] == []


def test_get_usage_without_a_session_returns_401(migrated_engine: AsyncEngine) -> None:
    client = TestClient(create_app(_settings(dsn=_dsn(migrated_engine))))

    response = client.get("/api/v1/usage")

    assert response.status_code == 401
