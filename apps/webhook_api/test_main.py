"""Unit test for the webhook_api FastAPI app's /healthz route.

Requires `REVIEW_WEBHOOK_API_ENVIRONMENT` to be set before
`apps.webhook_api.main` is imported, since its module-level `app` object
parses settings at import time — see this package's `conftest.py`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.webhook_api.main import app


def test_healthz_returns_ok() -> None:
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
