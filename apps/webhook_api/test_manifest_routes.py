"""Integration tests for the GitHub App manifest/callback setup flow,
driven through the actual apps/webhook_api FastAPI app via TestClient, with
GitHub's code-exchange endpoint stubbed via respx per
docs/rules/testing.md's "the GitHub adapter against a recorded cassette
(respx for httpx)" note.

Covers both the happy path (valid, unexpired, cookie-matching state,
returns credentials to the operator) and the negative path (a missing state
parameter, a state that doesn't match the signed cookie, and an expired
state are each rejected before any GitHub API call is made).
"""

from __future__ import annotations

import logging
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from adapter.github.manifest import generate_setup_state
from apps.webhook_api.main import create_app
from apps.webhook_api.manifest_routes import _STATE_COOKIE_NAME
from apps.webhook_api.settings import WebhookAPISettings

pytestmark = pytest.mark.integration

_GITHUB_API_BASE = "https://api.github.com"
_SETUP_SECRET = "integration-test-setup-secret"


def _build_client(*, setup_mode: bool = True, setup_state_ttl_seconds: float = 600.0) -> TestClient:
    settings = WebhookAPISettings(
        environment="development",
        github_webhook_secret=SecretStr("webhook-secret"),
        setup_mode=setup_mode,
        setup_state_secret=SecretStr(_SETUP_SECRET) if setup_mode else None,
        setup_state_ttl_seconds=setup_state_ttl_seconds,
        github_api_base_url=_GITHUB_API_BASE,
        _env_file=None,
    )
    return TestClient(create_app(settings))


def test_setup_mode_disabled_routes_are_not_registered() -> None:
    client = _build_client(setup_mode=False)

    start_response = client.get("/setup/start")
    callback_response = client.get("/setup/callback", params={"code": "x", "state": "y"})

    assert start_response.status_code == 404
    assert callback_response.status_code == 404


@respx.mock
def test_happy_path_returns_credentials_to_operator() -> None:
    client = _build_client()

    start_response = client.get("/setup/start")
    assert start_response.status_code == 200
    assert "onload" in start_response.text  # auto-submitting manifest form
    state = client.cookies.get(_STATE_COOKIE_NAME)
    assert state

    respx.post(f"{_GITHUB_API_BASE}/app-manifests/good-code/conversions").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 12345,
                "slug": "review-bot",
                "pem": "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
                "webhook_secret": "wh-secret-value",
                "client_id": "client-id-value",
                "client_secret": "client-secret-value",
            },
        )
    )

    response = client.get("/setup/callback", params={"code": "good-code", "state": state})

    assert response.status_code == 200
    body = response.json()
    assert body["app_id"] == 12345
    assert body["app_slug"] == "review-bot"
    assert body["private_key"].startswith("-----BEGIN RSA PRIVATE KEY-----")
    assert body["webhook_secret"] == "wh-secret-value"
    assert body["client_id"] == "client-id-value"
    assert body["client_secret"] == "client-secret-value"


def test_missing_state_rejected_before_github_call() -> None:
    client = _build_client()

    with respx.mock:
        response = client.get("/setup/callback", params={"code": "good-code"})

        assert response.status_code == 400
        assert len(respx.calls) == 0


def test_mismatched_state_rejected_before_github_call() -> None:
    client = _build_client()
    client.get("/setup/start")  # sets a real, validly-signed cookie

    with respx.mock:
        response = client.get(
            "/setup/callback",
            params={"code": "good-code", "state": "a-completely-different-state-value"},
        )

        assert response.status_code == 400
        assert len(respx.calls) == 0


def test_expired_state_rejected_before_github_call() -> None:
    # Crafts an already-expired signed state directly (same secret the
    # router is built with below) rather than driving it through a real
    # /setup/start call. Two approaches were tried and rejected: (1)
    # monkeypatching time.time() globally doesn't work because
    # adapter.github.manifest's `import time` binds the process-wide
    # stdlib module, which stdlib `logging` also calls once per LogRecord
    # created, silently consuming extra values from a fixed fake-clock
    # sequence; (2) driving expiry via a near-zero `setup_state_ttl_seconds`
    # doesn't work either because that value also sets the cookie's
    # `Max-Age`, and `Max-Age=0` (from `int(x)` truncating anything below 1
    # second to 0) tells the client's cookie jar to drop the cookie
    # immediately rather than store an already-expired one. Building the
    # value with a real, far-past `now` sidesteps both: verify_setup_state
    # is exercised with real, unmocked `time.time()` at the callback.
    client = _build_client(setup_state_ttl_seconds=600.0)
    expired_state = generate_setup_state(
        secret=_SETUP_SECRET.encode("utf-8"), now=time.time() - 700.0
    )
    client.cookies.set(_STATE_COOKIE_NAME, expired_state)

    with respx.mock:
        response = client.get(
            "/setup/callback", params={"code": "good-code", "state": expired_state}
        )

        assert response.status_code == 400
        assert len(respx.calls) == 0


@respx.mock
def test_github_code_exchange_failure_returns_502() -> None:
    client = _build_client()
    client.get("/setup/start")
    state = client.cookies.get(_STATE_COOKIE_NAME)

    respx.post(f"{_GITHUB_API_BASE}/app-manifests/bad-code/conversions").mock(
        return_value=httpx.Response(500, json={"message": "internal error"})
    )

    response = client.get("/setup/callback", params={"code": "bad-code", "state": state})

    assert response.status_code == 502


@respx.mock
def test_successful_callback_never_logs_credential_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Successful credential exchanges are logged without ever logging the
    private key/secret values themselves. The credentials below *are*
    returned in the HTTP response body (the intended delivery channel) but
    must never appear in any log record.
    """
    # See the matching comment in test_webhook_routes.py: adapter/storage's
    # Alembic env.py disables every already-created logger not listed in
    # alembic.ini once a migration/storage test has run earlier in the same
    # pytest process. Re-enabled explicitly for determinism.
    logging.getLogger("apps.webhook_api.manifest_routes").disabled = False

    client = _build_client()
    client.get("/setup/start")
    state = client.cookies.get(_STATE_COOKIE_NAME)

    private_key_value = "-----BEGIN RSA PRIVATE KEY-----\nsuper-secret-key-material\n-----END-----"
    webhook_secret_value = "super-secret-webhook-value"
    client_secret_value = "super-secret-client-value"
    respx.post(f"{_GITHUB_API_BASE}/app-manifests/good-code/conversions").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 999,
                "slug": "review-bot",
                "pem": private_key_value,
                "webhook_secret": webhook_secret_value,
                "client_id": "client-id-value",
                "client_secret": client_secret_value,
            },
        )
    )

    with caplog.at_level("INFO"):
        response = client.get("/setup/callback", params={"code": "good-code", "state": state})

    assert response.status_code == 200
    # The intended delivery channel — the response body — does carry them.
    assert response.json()["private_key"] == private_key_value
    # But no log record, at any point during this request, does.
    assert private_key_value not in caplog.text
    assert webhook_secret_value not in caplog.text
    assert client_secret_value not in caplog.text
    assert any(
        "github app setup callback succeeded" in record.getMessage() for record in caplog.records
    )
