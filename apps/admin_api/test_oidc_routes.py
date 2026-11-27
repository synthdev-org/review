"""Integration tests for apps/admin_api/oidc_routes.py — US-A24, driven
through the real app against a real Postgres (testcontainers), with a fake
``OIDCProvider`` standing in for the identity provider (the real adapter is
covered by ``adapter/auth/test_oidc.py``)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.admin_api.dependencies import CSRF_HEADER_NAME, SESSION_COOKIE_NAME
from apps.admin_api.main import create_app
from apps.admin_api.oidc_routes import OIDC_COOKIE_NAME
from apps.admin_api.settings import AdminAPISettings
from iam.oidc import OIDCError, OIDCIdentity, OIDCLoginRequest

pytestmark = pytest.mark.integration

_ADMIN = "integration-test-admin"
_PASSWORD = "correct-horse-battery-staple"
_REDIRECT = "http://testserver/api/v1/auth/oidc/callback"


class FakeOIDCProvider:
    def __init__(self, *, username: str | None = _ADMIN, fail: bool = False) -> None:
        self._username = username
        self._fail = fail

    async def authorization_url(self, *, login: OIDCLoginRequest, redirect_uri: str) -> str:
        return f"https://idp.example.com/authorize?state={login.state}"

    async def complete_login(
        self, *, code: str, login: OIDCLoginRequest, redirect_uri: str
    ) -> OIDCIdentity:
        if self._fail or self._username is None:
            raise OIDCError("boom")
        return OIDCIdentity(subject="sub-1", username=self._username)


def _settings(migrated_engine: AsyncEngine) -> AdminAPISettings:
    return AdminAPISettings(
        environment="development",
        database_url=SecretStr(migrated_engine.url.render_as_string(hide_password=False)),
        admin_bootstrap_username=_ADMIN,
        admin_bootstrap_password=SecretStr(_PASSWORD),
        admin_csrf_secret=SecretStr("integration-test-csrf-secret"),
        session_cookie_secure=False,
        oidc_redirect_uri=_REDIRECT,
        _env_file=None,
    )


def _start(client: TestClient) -> str:
    """Runs /login and returns the state the provider URL carries."""
    response = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://idp.example.com/authorize?state=")
    return response.headers["location"].split("state=")[1]


def test_valid_callback_for_an_active_user_issues_a_session(migrated_engine: AsyncEngine) -> None:
    app = create_app(_settings(migrated_engine), oidc_provider=FakeOIDCProvider())
    with TestClient(app) as client:
        state = _start(client)
        response = client.get(
            f"/api/v1/auth/oidc/callback?code=c&state={state}", follow_redirects=False
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"
        assert SESSION_COOKIE_NAME in client.cookies
        assert client.get("/api/v1/auth/session").json() == {"username": _ADMIN, "role": "admin"}

        # The session is a normal one: a mutating call with its CSRF token works.
        csrf = client.cookies["review_admin_csrf"]
        assert (
            client.post("/api/v1/auth/logout", headers={CSRF_HEADER_NAME: csrf}).status_code == 200
        )


def test_unknown_identity_is_rejected_and_no_user_is_created(
    migrated_engine: AsyncEngine,
) -> None:
    app = create_app(
        _settings(migrated_engine), oidc_provider=FakeOIDCProvider(username="stranger")
    )
    with TestClient(app) as client:
        state = _start(client)
        response = client.get(
            f"/api/v1/auth/oidc/callback?code=c&state={state}", follow_redirects=False
        )

        assert response.headers["location"] == "/login?error=sso_failed"
        assert SESSION_COOKIE_NAME not in client.cookies
        assert client.get("/api/v1/auth/session").status_code == 401


def test_deactivated_user_is_rejected(migrated_engine: AsyncEngine) -> None:
    settings = _settings(migrated_engine)
    with TestClient(create_app(settings)) as admin:
        admin.post("/api/v1/auth/login", json={"username": _ADMIN, "password": _PASSWORD})
        csrf = admin.cookies["review_admin_csrf"]
        created = admin.post(
            "/api/v1/users",
            json={"username": "gone", "password": "a-strong-enough-password", "role": "viewer"},
            headers={CSRF_HEADER_NAME: csrf},
        ).json()
        admin.patch(f"/api/v1/users/{created['id']}/deactivate", headers={CSRF_HEADER_NAME: csrf})

    with TestClient(
        create_app(settings, oidc_provider=FakeOIDCProvider(username="gone"))
    ) as client:
        state = _start(client)
        response = client.get(
            f"/api/v1/auth/oidc/callback?code=c&state={state}", follow_redirects=False
        )
        assert response.headers["location"] == "/login?error=sso_failed"
        assert SESSION_COOKIE_NAME not in client.cookies


@pytest.mark.parametrize("case", ["wrong_state", "no_cookie", "no_code", "provider_error"])
def test_bad_callbacks_are_rejected(migrated_engine: AsyncEngine, case: str) -> None:
    provider = FakeOIDCProvider(fail=case == "provider_error")
    with TestClient(create_app(_settings(migrated_engine), oidc_provider=provider)) as client:
        state = _start(client)
        if case == "no_cookie":
            client.cookies.delete(OIDC_COOKIE_NAME)
        url = {
            "wrong_state": "/api/v1/auth/oidc/callback?code=c&state=forged",
            "no_code": f"/api/v1/auth/oidc/callback?state={state}",
        }.get(case, f"/api/v1/auth/oidc/callback?code=c&state={state}")

        response = client.get(url, follow_redirects=False)

        assert response.headers["location"] == "/login?error=sso_failed"
        assert SESSION_COOKIE_NAME not in client.cookies


def test_oidc_disabled_reports_disabled_and_404s(migrated_engine: AsyncEngine) -> None:
    with TestClient(create_app(_settings(migrated_engine))) as client:
        assert client.get("/api/v1/auth/oidc/config").json() == {"enabled": False}
        assert client.get("/api/v1/auth/oidc/login", follow_redirects=False).status_code == 404
        assert client.get("/api/v1/auth/oidc/callback", follow_redirects=False).status_code == 404


def test_oidc_enabled_reports_enabled(migrated_engine: AsyncEngine) -> None:
    app = create_app(_settings(migrated_engine), oidc_provider=FakeOIDCProvider())
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/oidc/config").json() == {"enabled": True}
