"""Unit tests for the classifier write hook, the per-key hook map and the shared limiter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from adapter.egress.credentials import parse_credential_map
from apps.admin_api.classifier_routes import (
    classifier_audit_view,
    create_classifier_write_validator,
    target_problem,
)
from apps.admin_api.config_routes import create_config_router
from apps.admin_api.dependencies import AuthenticatedSession
from apps.admin_api.probe_rate_limit import SESSION_LIMIT_PER_MINUTE, ProbeRateLimiter
from iam.models import User
from tenancy.settings import (
    CLASSIFIER_PROVIDER_KEY,
    ConfigTree,
    SettingRow,
    SettingValidationError,
)

CREDS = parse_credential_map(
    SecretStr(
        '{"typesafe": {"token": "t", "allowed_hosts": ["api.typesafe.ai"]},'
        ' "other": {"token": "o", "allowed_hosts": ["other.example.com:8443"]}}'
    )
)


def _value(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "enabled": True,
        "endpoint_url": "https://api.typesafe.ai",
        "credential_ref": "typesafe",
        "mode": "shadow",
        "disclosure_acknowledged": True,
    }
    return base | overrides


def _detail(scope: str, value: dict[str, object], creds: Any = CREDS) -> str | None:
    async def run() -> None:
        await create_classifier_write_validator(creds)(scope, value)

    try:
        asyncio.run(run())
    except SettingValidationError as exc:
        return exc.detail
    return None


def test_a_bound_acknowledged_org_write_passes() -> None:
    assert _detail("org:o1", _value()) is None


@pytest.mark.parametrize("scope", ["platform", "repo:o1:r1", "team:o1:t1"])
def test_only_org_scope_is_accepted(scope: str) -> None:
    assert _detail(scope, _value()) == "org_scope_only"


def test_enabled_needs_the_acknowledgement_but_disabled_does_not() -> None:
    assert _detail("org:o1", _value(disclosure_acknowledged=False)) == "disclosure_not_acknowledged"
    assert _detail("org:o1", _value(enabled=False, disclosure_acknowledged=False)) is None


def test_disabling_succeeds_after_the_credential_is_unbound_but_enabling_does_not() -> None:
    gone = parse_credential_map(SecretStr('{"other": {"token": "o", "allowed_hosts": ["x.com"]}}'))
    assert _detail("org:o1", _value(enabled=False), creds=gone) is None
    assert _detail("org:o1", _value(enabled=False), creds=None) is None
    assert _detail("org:o1", _value(), creds=gone) is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1",
        "https://127.1",  # inet_aton shorthand, canonicalised first
        "https://2130706433",
        "https://10.1.2.3",
        "https://192.168.0.1",
        "https://172.16.0.9",
        "https://169.254.169.254",
        "https://100.100.100.200",
        "https://[::1]",
        "https://[::ffff:127.0.0.1]",
        "https://[fd00:ec2::254]",
        "https://0.0.0.0",
        "https://localhost",
        "https://app.localhost",
        "https://intranet",
    ],
)
def test_the_host_deny_list_runs_before_the_credential_binding(url: str) -> None:
    assert _detail("org:o1", _value(endpoint_url=url)) == "endpoint_host_denied"


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"credential_ref": "missing"}, "unknown_credential_ref"),
        ({"credential_ref": "other"}, "credential_host_not_bound"),
        ({"endpoint_url": "https://api.typesafe.ai.evil.com"}, "credential_host_not_bound"),
        ({"endpoint_url": "https://API.TypeSafe.AI./"}, None),  # same host once canonicalised
    ],
)
def test_the_credential_must_exist_and_be_bound_to_the_host(
    overrides: dict[str, object], problem: str | None
) -> None:
    assert _detail("org:o1", _value(**overrides)) == problem


def test_an_unavailable_credential_map_refuses_every_reference() -> None:
    assert (
        target_problem(None, "https://api.typesafe.ai", "typesafe") == "credential_map_unavailable"
    )


def test_the_audit_view_carries_host_ref_enabled_and_mode_only() -> None:
    view = classifier_audit_view(_value(model="m", disclosure_acknowledged=True))

    assert view == {
        "endpoint_host": "api.typesafe.ai",
        "credential_ref": "typesafe",
        "enabled": True,
        "mode": "shadow",
    }
    assert classifier_audit_view(None) is None


# --- the per-key hook map in config_routes (no I/O: fake repository) ---------


class _Repo:
    def __init__(self) -> None:
        self.rows: list[SettingRow] = []

    async def get_config_tree(self) -> ConfigTree:
        return ConfigTree(platform_settings={}, orgs=())

    async def list_rows_for_key(self, **_: object) -> tuple[SettingRow, ...]:
        return ()

    async def upsert(
        self,
        *,
        org_id: str | None,
        repo_id: str | None,
        team_id: str | None,
        key: str,
        value: dict[str, object],
    ) -> SettingRow:
        row = SettingRow(org_id=org_id, repo_id=repo_id, team_id=team_id, key=key, value=value)
        self.rows.append(row)
        return row


class _Audit:
    async def record(self, **_: Any) -> None:
        return None


def _admin() -> AuthenticatedSession:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    user = User(
        id="u",
        username="a",
        password_hash="x",
        role="admin",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    return AuthenticatedSession(session_id="s", user=user)


def _client(validators: dict[str, Any]) -> tuple[TestClient, _Repo]:
    async def session() -> AuthenticatedSession:
        return _admin()

    repo = _Repo()
    app = FastAPI()
    app.include_router(
        create_config_router(
            repository=repo,
            audit_log_repository=_Audit(),  # type: ignore[arg-type]
            require_session=session,  # type: ignore[arg-type]
            require_admin_role=session,
            write_validators=validators,
        )
    )
    return TestClient(app), repo


def test_each_key_runs_only_its_own_validator() -> None:
    calls: list[str] = []

    async def classifier(scope: str, value: dict[str, object]) -> None:
        calls.append(f"classifier:{scope}")

    async def knowledge(scope: str, value: dict[str, object]) -> None:
        calls.append("knowledge")

    client, repo = _client(
        {CLASSIFIER_PROVIDER_KEY: classifier, "product_knowledge_provider": knowledge}
    )
    body = {"value": _value() | {"backend": "typesafe", "model": "m"}}

    assert client.put("/api/v1/config/org:o1/classifier_provider", json=body).status_code == 200
    assert (
        client.put(
            "/api/v1/config/org:o1/memory_suppression_enabled", json={"value": {"enabled": True}}
        ).status_code
        == 200
    )

    assert calls == ["classifier:org:o1"]
    assert len(repo.rows) == 2


def test_a_validator_refusal_is_422_and_nothing_is_stored() -> None:
    async def refuse(scope: str, value: dict[str, object]) -> None:
        raise SettingValidationError(key=CLASSIFIER_PROVIDER_KEY, detail="nope")

    client, repo = _client({CLASSIFIER_PROVIDER_KEY: refuse})
    body = {"value": _value() | {"backend": "typesafe", "model": "m"}}

    response = client.put("/api/v1/config/org:o1/classifier_provider", json=body)

    assert (response.status_code, response.json()["detail"], repo.rows) == (422, "nope", [])


# --- the shared probe limiter ------------------------------------------------


def test_the_org_window_throttles_across_sessions_and_users() -> None:
    from fastapi import HTTPException, Response

    limiter = ProbeRateLimiter(clock=lambda: 0.0)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    def caller(n: int) -> AuthenticatedSession:
        user = User(
            id=f"u{n}",
            username="a",
            password_hash="x",
            role="admin",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        return AuthenticatedSession(session_id=f"s{n}", user=user)

    for n in range(SESSION_LIMIT_PER_MINUTE):
        limiter.charge(caller(n), Response(), org_id="org-1")
    with pytest.raises(HTTPException) as refused:
        limiter.charge(caller(99), Response(), org_id="org-1")
    limiter.charge(caller(99), Response(), org_id="org-2")  # another org is unaffected

    assert refused.value.status_code == 429


def test_has_room_never_allocates_state_for_a_checked_key() -> None:
    from apps.admin_api.probe_rate_limit import SlidingWindow

    window = SlidingWindow(limit=2, clock=lambda: 0.0)
    for n in range(100):
        assert window.has_room(f"arbitrary-{n}")
    assert window._hits == {}
    window.record("a")
    assert window.has_room("a") and list(window._hits) == ["a"]
