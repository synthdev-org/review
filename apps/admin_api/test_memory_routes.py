"""Integration tests for the Memory admin API
(``apps/admin_api/memory_routes.py``), driven through the actual
``apps/admin_api`` FastAPI app via ``TestClient``, against a real
Postgres/pgvector via ``testcontainers-python`` (per
``docs/rules/testing.md``'s mandatory DB-integration rule), per
``docs/plans/plan-deliver-admin-gui.md``'s Phase 3 (Unit D) Tests section:

"migrated wholesale from ``apps/webhook_api/test_admin_routes.py`` (same
``MemoryAdminRepository``/``PostedCommentReader``/``EmbeddingClient`` fakes
and 401/422/400/404 cases), plus new cases: the old route path no longer
exists on ``webhook_api`` at all ..., and approve/reject attribute the
acting session's user id."

Covers every US-A7/US-A8-A11 (US-M4/US-M5/US-M8/US-M10) ``Given/When/Then``:
approve requires/validates ``entry_type`` and computes a real embedding only
for ``dismissed_finding``; reject is idempotent and reversible in either
direction with approve; delete requires ``confirm=true`` and 404s on a
repeat call; list filters/paginates correctly; every route rejects an
unauthenticated request or one carrying the old, now fully retired static
token; every mutating route rejects a Viewer session; approve/reject
attribute the acting Admin's session identity (audit log +
``approved_by_user_id``).
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

from adapter.auth.argon2_hasher import Argon2PasswordHasher
from adapter.storage.engine import create_session_factory
from adapter.storage.models import (
    AdminAuditLogRow,
    AdminRole,
    AdminUserRow,
    MemoryEntryRow,
    MemoryEntryStatus,
    Org,
    PostedFindingCommentRow,
    Repo,
    Team,
)
from apps.admin_api.dependencies import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from apps.admin_api.main import create_app
from apps.admin_api.settings import AdminAPISettings

pytestmark = pytest.mark.integration

_BOOTSTRAP_USERNAME = "integration-test-admin"
_BOOTSTRAP_PASSWORD = "correct-horse-battery-staple"
_VIEWER_USERNAME = "integration-test-viewer"
_VIEWER_PASSWORD = "viewer-password-1234"
_EMBEDDING_DIMENSION = 384


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


def _login_as_bootstrap_admin(client: TestClient) -> str:
    """Logs the bootstrap admin in on ``client`` and returns the CSRF token
    every subsequent mutating request in these tests must echo back via
    ``CSRF_HEADER_NAME`` — mirrors
    ``apps/admin_api/test_user_routes.py``'s own identical helper."""
    client.post(
        "/api/v1/auth/login",
        json={"username": _BOOTSTRAP_USERNAME, "password": _BOOTSTRAP_PASSWORD},
    )
    return str(client.cookies[CSRF_COOKIE_NAME])


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
    `pending` row itself. Mirrors
    `apps/webhook_api/test_admin_routes.py`'s (deleted) own identical
    helper.
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


async def _seed_viewer(session_factory: async_sessionmaker[Any]) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            AdminUserRow(
                username=_VIEWER_USERNAME,
                password_hash=Argon2PasswordHasher().hash(_VIEWER_PASSWORD),
                role=AdminRole.VIEWER,
            )
        )


def test_list_without_a_session_returns_401(migrated_engine: AsyncEngine) -> None:
    client = TestClient(create_app(_settings(dsn=_dsn(migrated_engine))))

    response = client.get("/api/v1/memory-entries")

    assert response.status_code == 401


def test_list_with_the_old_retired_static_token_returns_401(migrated_engine: AsyncEngine) -> None:
    """US-A7's second Given/When/Then: the old static-token path is fully
    retired, not left as a silent alternate credential — a request carrying
    it (and no valid session) is rejected exactly like any other
    unauthenticated request."""
    client = TestClient(create_app(_settings(dsn=_dsn(migrated_engine))))

    response = client.get(
        "/api/v1/memory-entries",
        headers={"Authorization": "Bearer some-old-static-admin-api-key"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/memory-entries"),
        ("POST", f"/api/v1/memory-entries/{uuid.uuid4()}/approve"),
        ("POST", f"/api/v1/memory-entries/{uuid.uuid4()}/reject"),
        ("DELETE", f"/api/v1/memory-entries/{uuid.uuid4()}?confirm=true"),
    ],
)
def test_every_route_rejects_an_unauthenticated_request(
    migrated_engine: AsyncEngine, method: str, path: str
) -> None:
    client = TestClient(create_app(_settings(dsn=_dsn(migrated_engine))))

    response = client.request(method, path, json={"entry_type": "dismissed_finding"})

    assert response.status_code == 401


def test_list_empty_returns_200_with_empty_list_and_zero_total(
    migrated_engine: AsyncEngine,
) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login_as_bootstrap_admin(client)

        response = client.get("/api/v1/memory-entries", params={"repo_id": str(uuid.uuid4())})

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

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login_as_bootstrap_admin(client)
        response = client.get("/api/v1/memory-entries", params={"repo_id": str(repo_id)})
        response_wrong_status = client.get(
            "/api/v1/memory-entries", params={"repo_id": str(repo_id), "status": "approved"}
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

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login_as_bootstrap_admin(client)
        by_org = client.get("/api/v1/memory-entries", params={"org_id": str(org)})
        by_team = client.get("/api/v1/memory-entries", params={"team_id": str(team_id)})

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

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login_as_bootstrap_admin(client)
        first_page = client.get(
            "/api/v1/memory-entries", params={"repo_id": str(repo_id), "limit": 2, "offset": 0}
        ).json()
        second_page = client.get(
            "/api/v1/memory-entries", params={"repo_id": str(repo_id), "limit": 2, "offset": 2}
        ).json()

    assert first_page["total"] == 5
    assert second_page["total"] == 5
    assert len(first_page["entries"]) == 2
    assert len(second_page["entries"]) == 2
    first_ids = {e["id"] for e in first_page["entries"]}
    second_ids = {e["id"] for e in second_page["entries"]}
    assert first_ids.isdisjoint(second_ids)


def test_list_limit_above_the_cap_is_rejected(migrated_engine: AsyncEngine) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login_as_bootstrap_admin(client)
        response = client.get("/api/v1/memory-entries", params={"limit": 201})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_approve_missing_entry_type_is_rejected_and_entry_stays_pending(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8200)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        response = client.post(
            f"/api/v1/memory-entries/{entry_id}/approve",
            json={},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

        assert response.status_code == 422

        listed = client.get("/api/v1/memory-entries", params={"repo_id": str(repo_id)}).json()
    assert listed["entries"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_approve_invalid_entry_type_is_rejected(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8201)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        response = client.post(
            f"/api/v1/memory-entries/{entry_id}/approve",
            json={"entry_type": "not_a_real_type"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_approve_dismissed_finding_computes_and_stores_a_real_embedding(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8202)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        response = client.post(
            f"/api/v1/memory-entries/{entry_id}/approve",
            json={"entry_type": "dismissed_finding"},
            headers={CSRF_HEADER_NAME: csrf_token},
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

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        response = client.post(
            f"/api/v1/memory-entries/{entry_id}/approve",
            json={"entry_type": "accepted_fix"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["entry_type"] == "accepted_fix"
    assert body["embedding"] is None


def test_approve_unknown_entry_id_returns_404(migrated_engine: AsyncEngine) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        response = client.post(
            f"/api/v1/memory-entries/{uuid.uuid4()}/approve",
            json={"entry_type": "correction"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_reject_is_idempotent(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8300)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        first = client.post(
            f"/api/v1/memory-entries/{entry_id}/reject", headers={CSRF_HEADER_NAME: csrf_token}
        )
        second = client.post(
            f"/api/v1/memory-entries/{entry_id}/reject", headers={CSRF_HEADER_NAME: csrf_token}
        )

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

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        approved = client.post(
            f"/api/v1/memory-entries/{entry_id}/approve",
            json={"entry_type": "dismissed_finding"},
            headers={CSRF_HEADER_NAME: csrf_token},
        ).json()
        rejected = client.post(
            f"/api/v1/memory-entries/{entry_id}/reject", headers={CSRF_HEADER_NAME: csrf_token}
        ).json()
        reapproved = client.post(
            f"/api/v1/memory-entries/{entry_id}/approve",
            json={"entry_type": "correction"},
            headers={CSRF_HEADER_NAME: csrf_token},
        ).json()

    assert approved["status"] == "approved"
    assert rejected["status"] == "rejected"
    assert rejected["entry_type"] is None
    assert reapproved["status"] == "approved"
    assert reapproved["entry_type"] == "correction"
    assert reapproved["embedding"] is None


def test_reject_unknown_entry_id_returns_404(migrated_engine: AsyncEngine) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        response = client.post(
            f"/api/v1/memory-entries/{uuid.uuid4()}/reject", headers={CSRF_HEADER_NAME: csrf_token}
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_without_confirm_is_rejected_and_nothing_is_deleted(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8400)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        response = client.delete(
            f"/api/v1/memory-entries/{entry_id}", headers={CSRF_HEADER_NAME: csrf_token}
        )
        listed = client.get("/api/v1/memory-entries", params={"repo_id": str(repo_id)}).json()

    assert response.status_code == 400
    assert listed["total"] == 1


@pytest.mark.asyncio
async def test_delete_with_confirm_removes_the_row_and_404s_on_a_repeat_call(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8401)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        first = client.delete(
            f"/api/v1/memory-entries/{entry_id}",
            params={"confirm": "true"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        listed = client.get("/api/v1/memory-entries", params={"repo_id": str(repo_id)}).json()
        second = client.delete(
            f"/api/v1/memory-entries/{entry_id}",
            params={"confirm": "true"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    assert first.status_code == 200
    assert first.json() == {"deleted": True, "entry_id": entry_id}
    assert listed["total"] == 0
    assert second.status_code == 404


def test_delete_unknown_entry_id_returns_404_even_with_confirm(
    migrated_engine: AsyncEngine,
) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        response = client.delete(
            f"/api/v1/memory-entries/{uuid.uuid4()}",
            params={"confirm": "true"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    assert response.status_code == 404


def test_list_with_a_non_uuid_repo_id_filter_returns_400_not_500(
    migrated_engine: AsyncEngine,
) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        _login_as_bootstrap_admin(client)
        response = client.get("/api/v1/memory-entries", params={"repo_id": "not-a-uuid"})

    assert response.status_code == 400


def test_approve_with_a_non_uuid_entry_id_returns_400_not_500(
    migrated_engine: AsyncEngine,
) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        response = client.post(
            "/api/v1/memory-entries/not-a-uuid/approve",
            json={"entry_type": "correction"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    assert response.status_code == 400


def test_reject_with_a_non_uuid_entry_id_returns_400_not_500(
    migrated_engine: AsyncEngine,
) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        response = client.post(
            "/api/v1/memory-entries/not-a-uuid/reject", headers={CSRF_HEADER_NAME: csrf_token}
        )

    assert response.status_code == 400


def test_delete_with_a_non_uuid_entry_id_returns_400_not_500(
    migrated_engine: AsyncEngine,
) -> None:
    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        response = client.delete(
            "/api/v1/memory-entries/not-a-uuid",
            params={"confirm": "true"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    assert response.status_code == 400


# --- New cases (Phase 3, Unit D): Viewer parity/enforcement, session
# attribution, retired route no longer served by webhook_api ------------


@pytest.mark.asyncio
async def test_viewer_session_can_list_but_not_mutate(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    """US-A5's second clause: a Viewer session has read parity with an
    Admin (the list route is `require_session`-only) but every mutating
    route still rejects it with 403."""
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8500)
    await _seed_viewer(session_factory)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        client.post(
            "/api/v1/auth/login", json={"username": _VIEWER_USERNAME, "password": _VIEWER_PASSWORD}
        )
        viewer_csrf_token = client.cookies[CSRF_COOKIE_NAME]

        list_response = client.get("/api/v1/memory-entries", params={"repo_id": str(repo_id)})
        approve_response = client.post(
            f"/api/v1/memory-entries/{entry_id}/approve",
            json={"entry_type": "dismissed_finding"},
            headers={CSRF_HEADER_NAME: viewer_csrf_token},
        )
        reject_response = client.post(
            f"/api/v1/memory-entries/{entry_id}/reject",
            headers={CSRF_HEADER_NAME: viewer_csrf_token},
        )
        delete_response = client.delete(
            f"/api/v1/memory-entries/{entry_id}",
            params={"confirm": "true"},
            headers={CSRF_HEADER_NAME: viewer_csrf_token},
        )

    assert list_response.status_code == 200
    assert approve_response.status_code == 403
    assert reject_response.status_code == 403
    assert delete_response.status_code == 403


@pytest.mark.asyncio
async def test_approve_attributes_the_acting_admins_session_identity(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    """US-A7/US-A9: approve is attributed to the acting session's user, both
    in `memory_entry.approved_by_user_id` (Assumption 6) and in a written
    `admin_audit_log` row — the "someone with the shared key did this" gap
    US-A7 exists to close."""
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8501)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        response = client.post(
            f"/api/v1/memory-entries/{entry_id}/approve",
            json={"entry_type": "dismissed_finding"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["approved_by_user_id"] is not None

    async def _assert_attribution() -> None:
        async with session_factory() as session:
            admin_row = (
                await session.execute(
                    select(AdminUserRow).where(AdminUserRow.username == _BOOTSTRAP_USERNAME)
                )
            ).scalar_one()
            assert body["approved_by_user_id"] == str(admin_row.id)

            audit_row = (
                await session.execute(
                    select(AdminAuditLogRow).where(
                        AdminAuditLogRow.action == "memory_entry_approved",
                        AdminAuditLogRow.resource_id == entry_id,
                    )
                )
            ).scalar_one()
            assert audit_row.actor_user_id == admin_row.id

    await _assert_attribution()


@pytest.mark.asyncio
async def test_reject_clears_a_prior_approvals_attribution(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    """Assumption 6's entry-level `approved_by_user_id` field must reflect
    the entry's current approval state, not a stale prior one: an entry
    approved then later rejected must not still name the original
    approver in the `GET`/list response body, since a rejected entry's
    approval is no longer in effect."""
    entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8502)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        approve_response = client.post(
            f"/api/v1/memory-entries/{entry_id}/approve",
            json={"entry_type": "dismissed_finding"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        assert approve_response.status_code == 200
        assert approve_response.json()["approved_by_user_id"] is not None

        reject_response = client.post(
            f"/api/v1/memory-entries/{entry_id}/reject",
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    assert reject_response.status_code == 200
    assert reject_response.json()["approved_by_user_id"] is None


@pytest.mark.asyncio
async def test_reject_and_delete_are_recorded_in_the_audit_log(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
) -> None:
    reject_entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8502)
    delete_entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8503)

    with TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client:
        csrf_token = _login_as_bootstrap_admin(client)
        client.post(
            f"/api/v1/memory-entries/{reject_entry_id}/reject",
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        client.delete(
            f"/api/v1/memory-entries/{delete_entry_id}",
            params={"confirm": "true"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    async def _assert_actions_logged() -> None:
        async with session_factory() as session:
            actions = set(
                (
                    await session.execute(
                        select(AdminAuditLogRow.action).where(
                            AdminAuditLogRow.resource_id.in_([reject_entry_id, delete_entry_id])
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert actions == {"memory_entry_rejected", "memory_entry_deleted"}

    await _assert_actions_logged()


def test_the_old_static_token_route_no_longer_exists_on_webhook_api() -> None:
    """US-A7's second Given/When/Then: the static-token path is fully
    retired, not merely unregistered — `apps/webhook_api`'s own OpenAPI
    schema no longer lists `/admin/memory-entries` at all."""
    from apps.webhook_api.main import app as webhook_app

    schema = webhook_app.openapi()
    assert not any(path.startswith("/admin/memory-entries") for path in schema["paths"])


@pytest.mark.asyncio
async def test_approve_reject_delete_log_at_info(
    migrated_engine: AsyncEngine,
    session_factory: async_sessionmaker[Any],
    repo_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mirrors `apps/webhook_api/test_admin_routes.py`'s (deleted) own
    `test_approve_reject_delete_log_the_audit_trail_at_info` — the same
    application-log expectation, now emitted by
    `apps.admin_api.memory_routes` instead."""
    approve_entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8504)
    reject_entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8505)
    delete_entry_id = await _seed_pending_entry(session_factory, repo_id=repo_id, comment_id=8506)

    logging.getLogger("apps.admin_api.memory_routes").disabled = False
    with (
        caplog.at_level("INFO", logger="apps.admin_api.memory_routes"),
        TestClient(create_app(_settings(dsn=_dsn(migrated_engine)))) as client,
    ):
        csrf_token = _login_as_bootstrap_admin(client)
        client.post(
            f"/api/v1/memory-entries/{approve_entry_id}/approve",
            json={"entry_type": "correction"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        client.post(
            f"/api/v1/memory-entries/{reject_entry_id}/reject",
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        client.delete(
            f"/api/v1/memory-entries/{delete_entry_id}",
            params={"confirm": "true"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )

    admin_records = [r for r in caplog.records if r.name == "apps.admin_api.memory_routes"]

    approved = next(r for r in admin_records if r.getMessage() == "memory entry approved")
    assert getattr(approved, "entry_id", None) == approve_entry_id
    assert getattr(approved, "action", None) == "approve"

    rejected = next(r for r in admin_records if r.getMessage() == "memory entry rejected")
    assert getattr(rejected, "entry_id", None) == reject_entry_id
    assert getattr(rejected, "action", None) == "reject"

    deleted = next(r for r in admin_records if r.getMessage() == "memory entry deleted")
    assert getattr(deleted, "entry_id", None) == delete_entry_id
    assert getattr(deleted, "action", None) == "delete"
