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

Phase 10 (Unit K, US-A23) adds a second scenario to this same light
admin_api + postgres stack: bulk-approve/bulk-reject. Its own precondition
(existing ``pending`` ``memory_entry`` rows) is seeded with a direct SQL
insert over a raw ``asyncpg`` connection to the real, Compose-exposed
Postgres port — not an import of ``adapter.storage``'s ORM models, which
would conflict with this file's "never by importing application code
directly" rule above. That rule is about never substituting the
``admin_api`` HTTP entrypoint under test with an in-process shortcut; a raw
SQL precondition against the real database the real process itself reads
from is a different thing, and the same trade this file already makes by
staying scoped to admin_api + postgres rather than dragging in the full
webhook/worker pipeline just to produce a row whose provenance this
story's own Given/When/Then never mentions.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import httpx
import pytest

from tests.e2e.conftest import base_compose_env, compose, compose_down, wait_for_healthy

_ASSET_SCRIPT_RE = re.compile(r'src="\./assets/index-[\w-]+\.js"')
_DISCLOSURE_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "apps/admin_api/frontend/src/screens/productKnowledgeCopy.ts"
)
_NONEXISTENT_ENTRY_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def admin_ui_stack() -> Iterator[dict[str, str]]:
    # A credential map (token + host binding) so the Product knowledge panel
    # test can save a credentialed provider for wiki.invalid: an
    # unauthenticated provider must match the allow-list or a credential
    # host, and this stack configures no allow-list.
    env = base_compose_env(
        REVIEW_ADMIN_API_PRODUCT_KNOWLEDGE_CREDENTIALS=json.dumps(
            {"e2e": {"token": "x", "allowed_hosts": ["wiki.invalid"]}}
        )
    )
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


async def _seed_pending_memory_entries_async(env: dict[str, str], count: int) -> list[str]:
    """Inserts one ``org``/``repo`` row plus ``count`` ``pending``
    ``memory_entry`` rows (each with its own ``posted_finding_comment``
    parent, per that FK) directly against the real Postgres container this
    module's own ``admin_ui_stack`` fixture brings up — see this module's
    docstring for why a direct SQL seed, rather than driving the full
    webhook/worker pipeline, is the right scope for this story."""
    conn = await asyncpg.connect(
        host="localhost",
        port=int(env["POSTGRES_PORT"]),
        user=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
        database=env["POSTGRES_DB"],
    )
    try:
        org_id = uuid.uuid4()
        repo_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO org (id, name, slug) VALUES ($1, $2, $3)",
            org_id,
            "e2e-bulk-org",
            f"e2e-bulk-org-{org_id}",
        )
        await conn.execute(
            "INSERT INTO repo (id, org_id, name, full_name) VALUES ($1, $2, $3, $4)",
            repo_id,
            org_id,
            "widgets",
            "acme/widgets",
        )
        entry_ids: list[str] = []
        for i in range(count):
            comment_id = 9_000_000 + i
            await conn.execute(
                """
                INSERT INTO posted_finding_comment
                    (id, repo_id, pr_number, commit_sha, finding_file, finding_message)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                comment_id,
                repo_id,
                1,
                "f" * 40,
                "README.md",
                f"e2e bulk finding {i}",
            )
            entry_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO memory_entry
                    (id, repo_id, status, raw_signal_type, raw_signal_detail,
                     posted_finding_comment_id)
                VALUES ($1, $2, 'PENDING', 'reaction', '{}'::jsonb, $3)
                """,
                entry_id,
                repo_id,
                comment_id,
            )
            entry_ids.append(str(entry_id))
        return entry_ids
    finally:
        await conn.close()


def _seed_pending_memory_entries(env: dict[str, str], count: int) -> list[str]:
    return asyncio.run(_seed_pending_memory_entries_async(env, count))


def test_bulk_approve_and_reject_round_trip_over_the_real_api(
    admin_ui_stack: dict[str, str],
) -> None:
    """US-A23's own Given/When/Then (``docs/specs/spec-admin-gui.md``),
    proven against the real built stack (real Postgres, real ``admin_api``
    process) — the integration-tier `apps/admin_api/test_memory_routes.py`
    already proves the same contract against an in-process `TestClient`;
    this is the "the whole real system, not just the route handler" proof
    the traceability table's E2E row for this story now points at.

    Three pending entries are seeded (see `_seed_pending_memory_entries`);
    the first two are bulk-approved alongside one nonexistent id, the third
    is bulk-rejected alongside the same nonexistent id — the nonexistent
    id's own failure must never block or roll back its batch-mates, in
    either direction, exactly like the integration test's own scenario.
    """
    env = admin_ui_stack
    entry_ids = _seed_pending_memory_entries(env, count=3)

    with httpx.Client(base_url=_base_url(env)) as client:
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "username": env["REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_USERNAME"],
                "password": env["REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_PASSWORD"],
            },
        )
        assert login_response.status_code == 200
        csrf_token = client.cookies["review_admin_csrf"]

        # US-A23's first Given/When/Then: one explicit `entry_type` applied
        # to two real pending ids plus one nonexistent id in the same call.
        # `accepted_fix` (not `dismissed_finding`) deliberately skips the
        # embedding-computation branch — that path has its own dedicated
        # coverage (`apps/admin_api/test_memory_routes.py`'s
        # `test_approve_dismissed_finding_computes_and_stores_a_real_embedding`);
        # re-proving it here would just be slow duplication, not additional
        # proof of this story's own "a batch's one bad id doesn't block the
        # rest" behavior.
        bulk_approve_response = client.post(
            "/api/v1/memory-entries/bulk-approve",
            json={
                "entry_ids": [entry_ids[0], entry_ids[1], _NONEXISTENT_ENTRY_ID],
                "entry_type": "accepted_fix",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert bulk_approve_response.status_code == 200
        approve_results = {r["entry_id"]: r for r in bulk_approve_response.json()["results"]}
        assert approve_results[entry_ids[0]]["success"] is True
        assert approve_results[entry_ids[1]]["success"] is True
        assert approve_results[_NONEXISTENT_ENTRY_ID]["success"] is False

        # The very next read reflects both real writes — not just the
        # response body's own claim, the same "no restart involved" proof
        # the model-routing scenario above already makes for its own story.
        approved_listed = client.get("/api/v1/memory-entries?status=approved").json()
        approved_ids = {e["id"] for e in approved_listed["entries"]}
        assert {entry_ids[0], entry_ids[1]} <= approved_ids

        # US-A23's second Given/When/Then: bulk-reject the third pending id
        # plus the same nonexistent id — same partial-failure handling.
        bulk_reject_response = client.post(
            "/api/v1/memory-entries/bulk-reject",
            json={"entry_ids": [entry_ids[2], _NONEXISTENT_ENTRY_ID]},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert bulk_reject_response.status_code == 200
        reject_results = {r["entry_id"]: r for r in bulk_reject_response.json()["results"]}
        assert reject_results[entry_ids[2]]["success"] is True
        assert reject_results[_NONEXISTENT_ENTRY_ID]["success"] is False

        rejected_listed = client.get("/api/v1/memory-entries?status=rejected").json()
        assert entry_ids[2] in {e["id"] for e in rejected_listed["entries"]}

        # A mutating bulk call with no CSRF header is rejected (Assumption
        # 13) — proven here against the real dependency chain, same
        # posture as the model-routing scenario's own equivalent assertion.
        unauthenticated_bulk = client.post(
            "/api/v1/memory-entries/bulk-reject", json={"entry_ids": [entry_ids[0]]}
        )
        assert unauthenticated_bulk.status_code == 403


def test_product_knowledge_panel_round_trip_and_disclosure_in_built_bundle(
    admin_ui_stack: dict[str, str],
) -> None:
    """Phase 8 (Unit H, US-PK8/PK10/PK15): the endpoints the Product knowledge
    panel drives, against the real stack - save a provider, read back the
    server-computed ``effective`` value, list credentials, and probe a target
    no credential or allow-list covers (collapsed ``blocked_by_policy``, no
    egress) - and the panel's shared egress-disclosure literal ships in the
    real built bundle (read from the one TS constant the vitest assertion and
    the runbook also match).
    """
    literal_match = re.search(r'"([^"]+)"', _DISCLOSURE_SOURCE.read_text().split("=", 1)[1])
    assert literal_match is not None
    disclosure = literal_match.group(1)

    env = admin_ui_stack
    with httpx.Client(base_url=_base_url(env)) as client:
        index = client.get("/").text
        asset = _ASSET_SCRIPT_RE.search(index)
        assert asset is not None
        bundle = client.get(asset.group(0).split('"')[1].removeprefix(".")).text
        assert disclosure in bundle

        login = client.post(
            "/api/v1/auth/login",
            json={
                "username": env["REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_USERNAME"],
                "password": env["REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_PASSWORD"],
            },
        )
        assert login.status_code == 200
        csrf = {"X-CSRF-Token": client.cookies["review_admin_csrf"]}

        value = {
            "enabled": True,
            "display_name": "E2E Wiki",
            "endpoint_url": "http://wiki.invalid/mcp",
            "credential_ref": "e2e",
            "allow_public_repos": False,
            "issue_key_prefixes": ["HR"],
        }
        put = client.put(
            "/api/v1/config/platform/product_knowledge_provider",
            json={"value": value},
            headers=csrf,
        )
        assert put.status_code == 200, put.text

        tree = client.get("/api/v1/config").json()
        effective = tree["platform_effective"]["product_knowledge_provider"]
        assert effective["source"] == "platform"
        assert effective["value"]["display_name"] == "E2E Wiki"

        # A write the panel would map to a field: unknown credential ref.
        bad = client.put(
            "/api/v1/config/platform/product_knowledge_provider",
            json={"value": {**value, "credential_ref": "nope"}},
            headers=csrf,
        )
        assert bad.status_code == 422
        assert bad.json()["detail"] == "unknown_credential_ref"

        # An unauthenticated provider on a host no allow-list/credential
        # host covers is refused at write time.
        unlisted = client.put(
            "/api/v1/config/platform/product_knowledge_provider",
            json={
                "value": {
                    **value,
                    "endpoint_url": "http://other.invalid/mcp",
                    "credential_ref": None,
                }
            },
            headers=csrf,
        )
        assert unlisted.status_code == 422
        assert unlisted.json()["detail"] == "unauthenticated_host_not_allowed"

        credentials = client.get("/api/v1/product-knowledge/credentials")
        assert credentials.status_code == 200
        assert isinstance(credentials.json(), list)

        probe = client.post(
            "/api/v1/product-knowledge/test-connection",
            json={"endpoint_url": "http://other.invalid/mcp", "credential_ref": None},
            headers=csrf,
        )
        assert probe.status_code == 200, probe.text
        assert probe.json()["status"] == "blocked_by_policy"
        assert probe.json()["message"]
