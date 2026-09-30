"""Phase 8 E2E acceptance test: the real, Docker-image-built frontend
(``apps/admin_api/frontend/dist``, Assumption 9) served by the real
``apps/admin_api`` process, driven over real HTTP against a real Postgres
— per ``docs/plans/plan-deliver-admin-gui.md``'s Phase 8 section's own
Acceptance bullet: "the E2E test ... exercises the real built frontend
against the real backend for the full Phase 5 exit scenario."

Like every other test in this package (``docs/rules/testing.md``'s top
tier), this drives the same ``docker-compose.yml`` a developer runs
locally, over real HTTP/Docker state — never by importing application code
directly. Scoped to just the ``admin_api`` service and its own Compose
dependency chain (``postgres`` -> ``migrate`` -> ``admin_api``, per
``docker-compose.yml``'s own ``depends_on`` chain) rather than the whole
stack (``webhook_api``/``worker``), since this phase's acceptance scenario
(login -> model routing config read/write — the "Phase 5 exit scenario,"
Unit F, US-A14/US-A15/US-A16) never needs either of those.

"the real built frontend" is asserted two ways: the served HTML references
a real Vite-fingerprinted asset filename (never present for an unbuilt/
source tree — ``apps/admin_api/main.py`` would have skipped mounting the
frontend router entirely in that case, per
``apps/admin_api/frontend_routes.py``'s own "missing dist -> None, not a
crash" behavior), and a client-side route neither `apps/admin_api` nor any
static file literally defines (``/dashboard``) still resolves to that same
HTML — proving the SPA catch-all, not a directory listing or a 404.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest

from tests.e2e.conftest import base_compose_env, compose, compose_down, wait_for_healthy

_ASSET_SCRIPT_RE = re.compile(r'src="\./assets/index-[\w-]+\.js"')


@pytest.fixture
def admin_ui_stack() -> Iterator[dict[str, str]]:
    env = base_compose_env()
    compose_down(env)  # clean slate: no volume/container left by a prior run
    # Scoped to admin_api + its own dependency chain (postgres, migrate) —
    # Compose resolves `depends_on` automatically, so naming only
    # `admin_api` here is sufficient and keeps this test from also having
    # to wait on webhook_api/worker, which this phase's own acceptance
    # scenario never touches.
    result = compose("up", "-d", "--build", "admin_api", env=env)
    assert result.returncode == 0, (
        f"docker compose up failed unexpectedly.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    try:
        wait_for_healthy("admin_api", env)
        yield env
    finally:
        logs = compose("logs", "--no-color", "admin_api", env=env).stdout
        print(logs)  # surfaced by pytest on failure via -s/--capture=no, or -v tail
        compose_down(env)


def _base_url(env: dict[str, str]) -> str:
    return f"http://localhost:{env['ADMIN_API_PORT']}"


def test_real_built_frontend_is_served_at_root_and_for_client_side_routes(
    admin_ui_stack: dict[str, str],
) -> None:
    env = admin_ui_stack
    with httpx.Client(base_url=_base_url(env)) as client:
        root_response = client.get("/")
        assert root_response.status_code == 200
        assert "review Admin" in root_response.text
        # A Vite-fingerprinted asset reference only exists in a real
        # `npm run build` output — never present if `apps/admin_api/main.py`
        # had instead fallen back to its "frontend dist not found" skip
        # path (which would 404 here instead).
        assert _ASSET_SCRIPT_RE.search(root_response.text), root_response.text

        # An SPA client-side route (react-router, not a real server route
        # or static file) still resolves to the same shell — the catch-all
        # fallback, not a 404.
        dashboard_response = client.get("/dashboard")
        assert dashboard_response.status_code == 200
        assert dashboard_response.text == root_response.text

        # The fingerprinted JS asset itself is actually servable, not just
        # referenced.
        asset_path = _ASSET_SCRIPT_RE.search(root_response.text)
        assert asset_path is not None
        js_url = asset_path.group(0).split('"')[1].removeprefix(".")
        asset_response = client.get(js_url)
        assert asset_response.status_code == 200
        assert "javascript" in asset_response.headers["content-type"]


def test_login_and_model_routing_round_trip_over_the_real_api(
    admin_ui_stack: dict[str, str],
) -> None:
    """The "Phase 5 exit scenario" (Unit F, US-A14/US-A15/US-A16), driven
    over the real HTTP API the built frontend itself calls: login (US-A1),
    read the default (static-default) effective model per stage (US-A14),
    write a platform-wide override (US-A15), and confirm the read path
    reflects it with no restart (US-A16) — the same round trip
    apps/admin_api/frontend/src/screens/ModelRouting.tsx drives from the
    browser, exercised here against the real backend rather than a mocked
    one.
    """
    env = admin_ui_stack
    with httpx.Client(base_url=_base_url(env)) as client:
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "username": env["REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_USERNAME"],
                "password": env["REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_PASSWORD"],
            },
        )
        assert login_response.status_code == 200
        assert login_response.json()["role"] == "admin"
        assert "review_admin_session" in client.cookies
        csrf_token = client.cookies["review_admin_csrf"]

        # US-A14: before any write, every stage falls back to the static
        # default — no ModelRoutingConfig row exists yet on this fresh
        # database.
        initial = client.get("/api/v1/model-routing").json()
        intent_summary = next(s for s in initial["stages"] if s["stage"] == "intent_summary")
        assert intent_summary["source"] == "static_default"

        # US-A15: an Admin sets a platform-wide override for one stage.
        write_response = client.put(
            "/api/v1/model-routing/intent_summary",
            json={"org_id": None, "model_name": "e2e-test-override-model"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert write_response.status_code == 200
        assert write_response.json()["model_name"] == "e2e-test-override-model"

        # US-A16: the very next read reflects the change — no Worker/
        # Admin API restart involved anywhere in this test.
        updated = client.get("/api/v1/model-routing").json()
        updated_intent_summary = next(
            s for s in updated["stages"] if s["stage"] == "intent_summary"
        )
        assert updated_intent_summary["model_name"] == "e2e-test-override-model"
        assert updated_intent_summary["source"] == "platform"

        # A mutating request with no CSRF header is rejected (Assumption
        # 13) — proven here against the real dependency chain, not just
        # the unit-level fake in apps/admin_api/test_auth_routes.py.
        unauthenticated_write = client.put(
            "/api/v1/model-routing/intent_summary",
            json={"org_id": None, "model_name": "should-be-rejected"},
        )
        assert unauthenticated_write.status_code == 403

        logout_response = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token})
        assert logout_response.status_code == 200

        # The revoked session is rejected on the very next request — proves
        # logout (US-A2's counterpart) actually took effect against the
        # real session store, not just a client-side cookie clear.
        session_after_logout = client.get("/api/v1/auth/session")
        assert session_after_logout.status_code == 401
