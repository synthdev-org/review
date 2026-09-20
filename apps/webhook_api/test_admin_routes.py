"""Integration tests for the Memory admin API
(``apps/webhook_api/admin_routes.py``), driven through the actual
``apps/webhook_api`` FastAPI app via ``TestClient``, against a real
Postgres/pgvector via ``testcontainers-python`` (per
``docs/rules/testing.md``'s mandatory DB-integration rule), per
``docs/plans/plan-deliver-memory.md``'s Phase 5 (Unit E) Tests section.

Covers every US-M4/US-M5/US-M8/US-M10 ``Given/When/Then``: approve
requires/validates ``entry_type`` and computes a real embedding only for
``dismissed_finding``; reject is idempotent and reversible in either
direction with approve; delete requires ``confirm=true`` and 404s on a
repeat call; list filters/paginates correctly; every route 401s without a
valid key.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from adapter.storage.engine import create_session_factory
from adapter.storage.models import (
    MemoryEntryRow,
    MemoryEntryStatus,
    Org,
    PostedFindingCommentRow,
    Repo,
    Team,
)
from apps.webhook_api.main import create_app
from apps.webhook_api.settings import WebhookAPISettings

pytestmark = pytest.mark.integration

_ADMIN_KEY = "integration-test-admin-key"
_EMBEDDING_DIMENSION = 384


def _build_client(*, dsn: str, admin_api_key: str = _ADMIN_KEY) -> TestClient:
    settings = WebhookAPISettings(
        environment="development",
        database_url=SecretStr(dsn),
        github_webhook_secret=SecretStr("unused-webhook-secret"),
        github_app_id=1,
        github_app_private_key=SecretStr("unused-placeholder-key"),
        admin_api_key=SecretStr(admin_api_key),
        _env_file=None,
    )
    return TestClient(create_app(settings))


def _auth_headers(admin_api_key: str = _ADMIN_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_api_key}"}


@pytest_asyncio.fixture
async def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[Any]:
    return create_session_factory(migrated_engine)


@pytest_asyncio.fixture
async def repo_id(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(org_id=org.id, name="widgets", full_name=f"acme/widgets-{uuid.uuid4().hex}")
        session.add(repo)
        await session.flush()
        return repo.id


async def _seed_pending_entry(
    session_factory: async_sessionmaker[Any],
    *,
    repo_id: uuid.UUID,
    comment_id: int,
    file: str = "app/main.py",
    rule_id: str | None = "no-secrets",
    message: str = "a secret was found",
    status: MemoryEntryStatus = MemoryEntryStatus.PENDING,
    entry_type: str | None = None,
    org_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
) -> str:
    """Seeds a real `posted_finding_comment` row plus the `memory_entry`
    row that references it, directly via SQLAlchemy — the admin API only
    ever *acts on* already-captured entries; it never creates the initial
    `pending` row itself (that's Phases 2-4's own job), so these tests seed
    the starting state the same way `adapter/storage/test_memory_repository.py`
    does for Phase 1's own repository tests.
    """
    async with session_factory() as session, session.begin():
        session.add(
            PostedFindingCommentRow(
                id=comment_id,
                repo_id=repo_id,
                pr_number=7,
                commit_sha="a" * 40,
                finding_file=file,
                finding_line=10,
                finding_rule_id=rule_id,
                finding_message=message,
            )
        )
    async with session_factory() as session, session.begin():
        entry = MemoryEntryRow(
            repo_id=repo_id,
            org_id=org_id,
            team_id=team_id,
            posted_finding_comment_id=comment_id,
            raw_signal_type="reaction",
            raw_signal_detail={"emoji": "+1"},
            github_comment_id=None,
            status=status,
            entry_type=entry_type,
        )
        session.add(entry)
        await session.flush()
        return str(entry.id)


def test_list_without_authorization_header_returns_401(migrated_engine: AsyncEngine) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.get("/admin/memory-entries")

    assert response.status_code == 401


def test_list_with_wrong_key_returns_401(migrated_engine: AsyncEngine) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.get("/admin/memory-entries", headers={"Authorization": "Bearer wrong-key"})

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/admin/memory-entries"),
        ("POST", f"/admin/memory-entries/{uuid.uuid4()}/approve"),
        ("POST", f"/admin/memory-entries/{uuid.uuid4()}/reject"),
        ("DELETE", f"/admin/memory-entries/{uuid.uuid4()}?confirm=true"),
    ],
)
def test_every_route_rejects_an_unauthenticated_request(
    migrated_engine: AsyncEngine, method: str, path: str
) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.request(method, path, json={"entry_type": "dismissed_finding"})

    assert response.status_code == 401


def test_list_empty_returns_200_with_empty_list_and_zero_total(
    migrated_engine: AsyncEngine,
) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.get(
        "/admin/memory-entries",
        params={"repo_id": str(uuid.uuid4())},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {"entries": [], "total": 0}


@pytest.mark.asyncio
async def test_list_filters_by_repo_and_status(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    other_repo_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        org = Org(name="other-org", slug=f"other-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        session.add(
            Repo(
                id=other_repo_id,
                org_id=org.id,
                name="other",
                full_name=f"other/other-{uuid.uuid4().hex}",
            )
        )

    await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8001)
    await _seed_pending_entry(session_factory, repo_id=other_repo_id, comment_id=8002)

    # `with client:` reuses one anyio portal (one event loop) across both
    # calls below — without it, each `TestClient` call opens its own
    # ephemeral portal, and the pooled asyncpg connection this route's
    # first DB call opens under one loop cannot be reused under another
    # (the same pattern `apps/webhook_api/test_webhook_routes.py` already
    # establishes for a multi-call test against the same client).
    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        response = client.get(
            "/admin/memory-entries", params={"repo_id": str(repo_id)}, headers=_auth_headers()
        )
        response_wrong_status = client.get(
            "/admin/memory-entries",
            params={"repo_id": str(repo_id), "status": "approved"},
            headers=_auth_headers(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["entries"]) == 1
    assert body["entries"][0]["posted_finding_comment_id"] == 8001
    assert response_wrong_status.json() == {"entries": [], "total": 0}


@pytest.mark.asyncio
async def test_list_filters_by_org_id_and_team_id(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    """`org_id`/`team_id` are documented as filter parameters in their own
    right, not merely an alias for `repo_id` — a real regression an earlier
    plan draft introduced (see `spec-memory.md`'s "Admin key scope"
    clarification). Seeds two entries under the same repo but different
    `org_id`/`team_id` scope and proves each filter isolates independently
    over HTTP, through the real admin route, not just at the repository
    layer."""
    async with session_factory() as session, session.begin():
        org = (await session.execute(select(Repo.org_id).where(Repo.id == repo_id))).scalar_one()
        team = Team(org_id=org, name=f"team-{uuid.uuid4().hex}")
        other_org = Org(name="other-org", slug=f"other-{uuid.uuid4().hex}")
        session.add_all([team, other_org])
        await session.flush()
        team_id, other_org_id = team.id, other_org.id

    await _seed_pending_entry(
        session_factory, repo_id=repo_id, comment_id=8010, org_id=org, team_id=team_id
    )
    await _seed_pending_entry(
        session_factory, repo_id=repo_id, comment_id=8011, org_id=other_org_id, team_id=None
    )

    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        by_org = client.get(
            "/admin/memory-entries", params={"org_id": str(org)}, headers=_auth_headers()
        )
        by_team = client.get(
            "/admin/memory-entries", params={"team_id": str(team_id)}, headers=_auth_headers()
        )

    assert by_org.status_code == 200
    assert by_org.json()["total"] == 1
    assert by_org.json()["entries"][0]["posted_finding_comment_id"] == 8010

    assert by_team.status_code == 200
    assert by_team.json()["total"] == 1
    assert by_team.json()["entries"][0]["posted_finding_comment_id"] == 8010


@pytest.mark.asyncio
async def test_list_paginates_and_returns_the_total_matching_count(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    for i in range(5):
        await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8100 + i)

    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        first_page = client.get(
            "/admin/memory-entries",
            params={"repo_id": str(repo_id), "limit": 2, "offset": 0},
            headers=_auth_headers(),
        ).json()
        second_page = client.get(
            "/admin/memory-entries",
            params={"repo_id": str(repo_id), "limit": 2, "offset": 2},
            headers=_auth_headers(),
        ).json()

    assert first_page["total"] == 5
    assert second_page["total"] == 5
    assert len(first_page["entries"]) == 2
    assert len(second_page["entries"]) == 2
    first_ids = {e["id"] for e in first_page["entries"]}
    second_ids = {e["id"] for e in second_page["entries"]}
    assert first_ids.isdisjoint(second_ids)


def test_list_limit_above_the_cap_is_rejected(migrated_engine: AsyncEngine) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.get("/admin/memory-entries", params={"limit": 201}, headers=_auth_headers())

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_approve_missing_entry_type_is_rejected_and_entry_stays_pending(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8200)
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.post(
        f"/admin/memory-entries/{entry_id}/approve", json={}, headers=_auth_headers()
    )

    assert response.status_code == 422

    listed = client.get(
        "/admin/memory-entries", params={"repo_id": str(repo_id)}, headers=_auth_headers()
    ).json()
    assert listed["entries"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_approve_invalid_entry_type_is_rejected(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8201)
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.post(
        f"/admin/memory-entries/{entry_id}/approve",
        json={"entry_type": "not_a_real_type"},
        headers=_auth_headers(),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_approve_dismissed_finding_computes_and_stores_a_real_embedding(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8202)
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.post(
        f"/admin/memory-entries/{entry_id}/approve",
        json={"entry_type": "dismissed_finding"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["entry_type"] == "dismissed_finding"
    assert body["approved_at"] is not None
    assert body["embedding"] is not None
    assert len(body["embedding"]) == _EMBEDDING_DIMENSION


@pytest.mark.asyncio
async def test_approve_accepted_fix_computes_no_embedding(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8203)
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.post(
        f"/admin/memory-entries/{entry_id}/approve",
        json={"entry_type": "accepted_fix"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["entry_type"] == "accepted_fix"
    assert body["embedding"] is None


def test_approve_unknown_entry_id_returns_404(migrated_engine: AsyncEngine) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.post(
        f"/admin/memory-entries/{uuid.uuid4()}/approve",
        json={"entry_type": "correction"},
        headers=_auth_headers(),
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_reject_is_idempotent(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8300)

    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        first = client.post(f"/admin/memory-entries/{entry_id}/reject", headers=_auth_headers())
        second = client.post(f"/admin/memory-entries/{entry_id}/reject", headers=_auth_headers())

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_approve_and_reject_flip_classification_in_either_direction(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8301)

    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        approved = client.post(
            f"/admin/memory-entries/{entry_id}/approve",
            json={"entry_type": "dismissed_finding"},
            headers=_auth_headers(),
        ).json()
        rejected = client.post(
            f"/admin/memory-entries/{entry_id}/reject", headers=_auth_headers()
        ).json()
        reapproved = client.post(
            f"/admin/memory-entries/{entry_id}/approve",
            json={"entry_type": "correction"},
            headers=_auth_headers(),
        ).json()

    assert approved["status"] == "approved"
    assert rejected["status"] == "rejected"
    assert rejected["entry_type"] is None
    assert reapproved["status"] == "approved"
    assert reapproved["entry_type"] == "correction"
    assert reapproved["embedding"] is None


@pytest.mark.asyncio
async def test_approve_reject_delete_log_the_audit_trail_at_info(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`spec-memory.md`'s Observability section names an audit trail for
    every approve/reject/delete call (entry id, action, timestamp); the
    `timestamp` field is added by `JSONFormatter` for every log record, not
    a per-call concern, so this test proves the `entry_id`/`action` fields
    this route itself is responsible for are present and correct."""
    approve_entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8302)
    reject_entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8303)
    delete_entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8304)

    logging.getLogger("apps.webhook_api.admin_routes").disabled = False
    with (
        caplog.at_level("INFO", logger="apps.webhook_api.admin_routes"),
        _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False))) as client,
    ):
        client.post(
            f"/admin/memory-entries/{approve_entry_id}/approve",
            json={"entry_type": "correction"},
            headers=_auth_headers(),
        )
        client.post(f"/admin/memory-entries/{reject_entry_id}/reject", headers=_auth_headers())
        client.delete(
            f"/admin/memory-entries/{delete_entry_id}?confirm=true", headers=_auth_headers()
        )

    admin_records = [r for r in caplog.records if r.name == "apps.webhook_api.admin_routes"]

    approved = next(r for r in admin_records if r.getMessage() == "memory entry approved")
    assert getattr(approved, "entry_id", None) == approve_entry_id
    assert getattr(approved, "action", None) == "approve"

    rejected = next(r for r in admin_records if r.getMessage() == "memory entry rejected")
    assert getattr(rejected, "entry_id", None) == reject_entry_id
    assert getattr(rejected, "action", None) == "reject"

    deleted = next(r for r in admin_records if r.getMessage() == "memory entry deleted")
    assert getattr(deleted, "entry_id", None) == delete_entry_id
    assert getattr(deleted, "action", None) == "delete"


def test_reject_unknown_entry_id_returns_404(migrated_engine: AsyncEngine) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.post(f"/admin/memory-entries/{uuid.uuid4()}/reject", headers=_auth_headers())

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_without_confirm_is_rejected_and_nothing_is_deleted(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8400)

    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        response = client.delete(f"/admin/memory-entries/{entry_id}", headers=_auth_headers())
        listed = client.get(
            "/admin/memory-entries", params={"repo_id": str(repo_id)}, headers=_auth_headers()
        ).json()

    assert response.status_code == 400
    assert listed["total"] == 1


@pytest.mark.asyncio
async def test_delete_with_confirm_removes_the_row_and_404s_on_a_repeat_call(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8401)

    with _build_client(
        dsn=str(migrated_engine.url.render_as_string(hide_password=False))
    ) as client:
        first = client.delete(
            f"/admin/memory-entries/{entry_id}",
            params={"confirm": "true"},
            headers=_auth_headers(),
        )
        listed = client.get(
            "/admin/memory-entries", params={"repo_id": str(repo_id)}, headers=_auth_headers()
        ).json()
        second = client.delete(
            f"/admin/memory-entries/{entry_id}",
            params={"confirm": "true"},
            headers=_auth_headers(),
        )

    assert first.status_code == 200
    assert first.json() == {"deleted": True, "entry_id": entry_id}
    assert listed["total"] == 0
    assert second.status_code == 404


def test_delete_unknown_entry_id_returns_404_even_with_confirm(
    migrated_engine: AsyncEngine,
) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.delete(
        f"/admin/memory-entries/{uuid.uuid4()}",
        params={"confirm": "true"},
        headers=_auth_headers(),
    )

    assert response.status_code == 404


def test_list_with_a_non_uuid_repo_id_filter_returns_400_not_500(
    migrated_engine: AsyncEngine,
) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.get(
        "/admin/memory-entries", params={"repo_id": "not-a-uuid"}, headers=_auth_headers()
    )

    assert response.status_code == 400


def test_approve_with_a_non_uuid_entry_id_returns_400_not_500(
    migrated_engine: AsyncEngine,
) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.post(
        "/admin/memory-entries/not-a-uuid/approve",
        json={"entry_type": "correction"},
        headers=_auth_headers(),
    )

    assert response.status_code == 400


def test_reject_with_a_non_uuid_entry_id_returns_400_not_500(
    migrated_engine: AsyncEngine,
) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.post("/admin/memory-entries/not-a-uuid/reject", headers=_auth_headers())

    assert response.status_code == 400


def test_delete_with_a_non_uuid_entry_id_returns_400_not_500(
    migrated_engine: AsyncEngine,
) -> None:
    client = _build_client(dsn=str(migrated_engine.url.render_as_string(hide_password=False)))

    response = client.delete(
        "/admin/memory-entries/not-a-uuid",
        params={"confirm": "true"},
        headers=_auth_headers(),
    )

    assert response.status_code == 400
