"""Integration tests for the GitHub webhook receiver route, driven through
the actual apps/webhook_api FastAPI app via TestClient (no real network
socket, per docs/rules/testing.md, but exercises routing/request-parsing
beyond adapter.github.signature's pure verification function).

Per the plan's Phase 4 Tests section: "the same three failure cases plus
the happy path returning 200/401; a malformed-JSON or missing-installation-
ID payload that still carries a valid signature returns a clean 4xx rather
than an unhandled 500; an oversized body and a rate-limit-exceeding burst
are both rejected before signature verification runs."
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from apps.webhook_api.main import create_app
from apps.webhook_api.settings import WebhookAPISettings

pytestmark = pytest.mark.integration

_SECRET = "integration-test-webhook-secret"
_URL = "/webhooks/github"


def _sign(body: bytes, secret: str = _SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _build_client(
    *,
    max_body_bytes: int = 1_000_000,
    rate_limit_max_requests: int = 1_000,
    rate_limit_window_seconds: float = 60.0,
) -> TestClient:
    settings = WebhookAPISettings(
        environment="development",
        github_webhook_secret=SecretStr(_SECRET),
        webhook_max_body_bytes=max_body_bytes,
        webhook_rate_limit_max_requests=rate_limit_max_requests,
        webhook_rate_limit_window_seconds=rate_limit_window_seconds,
        _env_file=None,
    )
    return TestClient(create_app(settings))


def _valid_payload_bytes() -> bytes:
    return json.dumps({"action": "opened", "installation": {"id": 123}}).encode("utf-8")


def test_valid_signature_returns_200() -> None:
    client = _build_client()
    body = _valid_payload_bytes()

    response = client.post(_URL, content=body, headers={"X-Hub-Signature-256": _sign(body)})

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_wrong_secret_signature_returns_401() -> None:
    client = _build_client()
    body = _valid_payload_bytes()

    response = client.post(
        _URL,
        content=body,
        headers={"X-Hub-Signature-256": _sign(body, secret="a-totally-different-secret")},
    )

    assert response.status_code == 401


def test_tampered_payload_returns_401() -> None:
    client = _build_client()
    body = _valid_payload_bytes()
    signature = _sign(body)
    tampered_body = body.replace(b"123", b"999")

    response = client.post(_URL, content=tampered_body, headers={"X-Hub-Signature-256": signature})

    assert response.status_code == 401


def test_missing_signature_header_returns_401() -> None:
    client = _build_client()

    response = client.post(_URL, content=_valid_payload_bytes())

    assert response.status_code == 401


def test_oversized_body_returns_413_before_signature_check() -> None:
    client = _build_client(max_body_bytes=10)
    body = _valid_payload_bytes()
    assert len(body) > 10

    # Deliberately an *invalid* signature: proves the 413 fires from the
    # body-size limit, not incidentally from signature verification having
    # run first and separately rejected it with a 401.
    response = client.post(_URL, content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"})

    assert response.status_code == 413


def test_rate_limit_exceeding_burst_returns_429() -> None:
    client = _build_client(rate_limit_max_requests=2)
    body = _valid_payload_bytes()
    headers = {"X-Hub-Signature-256": _sign(body)}

    first = client.post(_URL, content=body, headers=headers)
    second = client.post(_URL, content=body, headers=headers)
    third = client.post(_URL, content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429


def test_rate_limit_fires_before_signature_verification() -> None:
    """Proves the rejection ORDER, not just that both checks individually
    work: every request below carries an invalid signature, so if signature
    verification ran first every response would be 401 regardless of the
    rate limit. Getting 429 on the third request instead proves the rate
    limiter runs — and rejects — before signature verification does, per
    the plan's Phase 4 section and the module docstring's stated ordering.
    """
    client = _build_client(rate_limit_max_requests=2)
    body = _valid_payload_bytes()
    headers = {"X-Hub-Signature-256": "sha256=deadbeef"}

    first = client.post(_URL, content=body, headers=headers)
    second = client.post(_URL, content=body, headers=headers)
    third = client.post(_URL, content=body, headers=headers)

    assert first.status_code == 401
    assert second.status_code == 401
    assert third.status_code == 429


def test_malformed_json_with_valid_signature_returns_clean_4xx() -> None:
    client = _build_client()
    body = b"this is not json"

    response = client.post(_URL, content=body, headers={"X-Hub-Signature-256": _sign(body)})

    assert 400 <= response.status_code < 500
    assert response.json()["detail"]


def test_missing_installation_id_with_valid_signature_returns_clean_4xx() -> None:
    client = _build_client()
    body = json.dumps({"action": "opened"}).encode("utf-8")

    response = client.post(_URL, content=body, headers={"X-Hub-Signature-256": _sign(body)})

    assert 400 <= response.status_code < 500
    assert response.json()["detail"]


def test_rejected_webhook_log_never_contains_raw_payload_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Per the plan's Observability & rollback section: rejections are
    logged "never the payload body, which may carry sensitive repo/org
    data". A distinctive marker embedded in the (invalidly signed) body
    below must not leak into any log record emitted while handling it.
    """
    # adapter/storage's Alembic env.py calls logging.config.fileConfig(),
    # which (default disable_existing_loggers=True) disables every
    # already-created logger not explicitly listed in alembic.ini —
    # including this one, if a migration/storage test happened to run
    # earlier in the same pytest process (they do, alphabetically, in the
    # full integration run). Re-enabling explicitly keeps this test
    # deterministic regardless of test order, without touching Phase 2's
    # (unrelated, pre-existing) Alembic logging config.
    logging.getLogger("apps.webhook_api.webhook_routes").disabled = False

    client = _build_client()
    marker = "definitely-sensitive-repo-name-marker"
    body = json.dumps({"installation": {"id": 123}, "secret_repo": marker}).encode("utf-8")

    with caplog.at_level("WARNING"):
        response = client.post(
            _URL, content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"}
        )

    assert response.status_code == 401
    assert marker not in caplog.text
    assert any("webhook rejected" in record.getMessage() for record in caplog.records)
