"""The indexing delivery plan's Phase 7 (Unit G) one new E2E scenario, per
that phase's own Tests section: "driving the full Docker Compose stack
(real embedding model, real Postgres) with a synthetic `push` webhook
against a stubbed GitHub endpoint, asserting chunks/embeddings/edges land
in Postgres for the pushed commit."

Per docs/rules/testing.md's top tier: no shortcuts that bypass the Webhook
API's actual HTTP entrypoint — a real signed `push` webhook is POSTed to
the running `webhook_api` container, which enqueues a real `INDEX` job the
running `worker` container claims and processes through the real
`indexing.pipeline.run_indexing_job`, using the real bundled ONNX embedding
model baked into the image (no LLM Gateway call is on this path at all,
per ADR-0007) and a real Postgres/pgvector instance. GitHub itself is
stood in for by a local stub server (`stub_github_server`), the same
pattern `tests/e2e/test_review_pipeline.py` already established for the
`REVIEW` path — reused here for the Git Trees/Blobs API calls
`adapter.github.tree.GitHubTreeFetcher` makes instead of that file's
diff-fetch/posting call chain.

One Compose stack per module (`running_stack`), same reasoning as
`test_review_pipeline.py`'s own module-scoped fixture: bringing the stack
up is expensive, and each test function uses its own distinct commit SHA
so idempotency/dedupe never interferes across cases.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from base64 import b64encode
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast

import asyncpg
import pytest

from tests.e2e.conftest import (
    base_compose_env,
    compose,
    compose_down,
    wait_for_healthy,
    wait_for_log_line,
)

STUB_GITHUB_PORT = 19081
_OWNER_REPO = "acme/indexing-e2e"
_INSTALLATION_ID = 654321
_WEBHOOK_URL_PATH = "/webhooks/github"
_WEBHOOK_SECRET = "e2e-test-webhook-secret"
_WAIT_TIMEOUT_SECONDS = 120.0

# A real, small TypeScript file — one exported function calling another —
# so US3 (chunking), US4 (embedding), and US5 (graph extraction, including
# a real call edge) all have something genuine to produce, not just an
# empty/no-op job.
_APP_TS = b"""export function helper(): number {
  return 1;
}

export function caller(): number {
  return helper();
}
"""
_APP_BLOB_SHA = "a" * 40


# ---------------------------------------------------------------------------
# Stub GitHub server — Git Trees/Blobs API + installation-token exchange
# only (the calls `adapter.github.tree.GitHubTreeFetcher` makes), per this
# module's own docstring.
# ---------------------------------------------------------------------------


@dataclass
class _CallLog:
    calls: list[str] = field(default_factory=list)


class _StubGitHubHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def _calls(self) -> _CallLog:
        calls = getattr(self.server, "calls", None)
        assert isinstance(calls, _CallLog)
        return calls

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = self.path
        self._calls().calls.append(f"POST {path}")
        if re.fullmatch(r"/app/installations/\d+/access_tokens", path):
            self._send_json(
                201, {"token": f"ghs_idx_{uuid.uuid4().hex}", "expires_at": "2999-01-01T00:00:00Z"}
            )
            return
        self._send_json(404, {"message": "not found (stub)"})

    def do_GET(self) -> None:
        path_only = self.path.split("?", 1)[0]
        self._calls().calls.append(f"GET {self.path}")
        tree = getattr(self.server, "tree")  # noqa: B009
        blobs = getattr(self.server, "blobs")  # noqa: B009

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/git/trees/\w+", path_only):
            self._send_json(200, {"tree": tree, "truncated": False})
            return

        match = re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/git/blobs/(\w+)", path_only)
        if match:
            content = blobs.get(match.group(1), b"")
            self._send_json(
                200, {"content": b64encode(content).decode("ascii"), "encoding": "base64"}
            )
            return

        self._send_json(404, {"message": "not found (stub)"})


@pytest.fixture(scope="module")
def stub_github_server() -> Iterator[HTTPServer]:
    server = HTTPServer(("0.0.0.0", STUB_GITHUB_PORT), _StubGitHubHandler)
    server.tree = [  # type: ignore[attr-defined]
        {
            "type": "blob",
            "path": "app.ts",
            "mode": "100644",
            "sha": _APP_BLOB_SHA,
            "size": len(_APP_TS),
        }
    ]
    server.blobs = {_APP_BLOB_SHA: _APP_TS}  # type: ignore[attr-defined]
    server.calls = _CallLog()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Compose stack — one per module.
# ---------------------------------------------------------------------------


def _e2e_env() -> dict[str, str]:
    return base_compose_env(
        REVIEW_WORKER_GITHUB_API_BASE_URL=f"http://host.docker.internal:{STUB_GITHUB_PORT}",
        REVIEW_WORKER_POLL_INTERVAL_SECONDS="0.2",
        # The one flag this whole scenario hinges on — Phases 1-6 ship
        # inert by default (Assumption 11); this is the affirmative,
        # single-config-value opt-in Phase 7's own Compatibility & rollout
        # section describes.
        REVIEW_WEBHOOK_API_INDEXING_ENABLED="true",
    )


@pytest.fixture(scope="module")
def running_stack(
    stub_llm_server: HTTPServer, stub_github_server: HTTPServer
) -> Iterator[dict[str, str]]:
    env = _e2e_env()
    compose_down(env)
    result = compose("up", "-d", "--build", env=env)
    assert result.returncode == 0, (
        f"docker compose up failed unexpectedly.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    try:
        wait_for_healthy("webhook_api", env)
        wait_for_log_line("worker", env, "worker started")
        yield env
    finally:
        logs = compose("logs", "--no-color", env=env).stdout
        print(logs)
        compose_down(env)


# ---------------------------------------------------------------------------
# Webhook signing / posting.
# ---------------------------------------------------------------------------


def _sign(body: bytes, secret: str = _WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _push_payload(*, commit_sha: str) -> bytes:
    return json.dumps(
        {
            "installation": {"id": _INSTALLATION_ID},
            "repository": {"full_name": _OWNER_REPO, "default_branch": "main"},
            "ref": "refs/heads/main",
            "before": "0" * 40,
            "after": commit_sha,
            "commits": [{"added": ["app.ts"], "removed": [], "modified": []}],
            "forced": False,
        }
    ).encode()


def _post_webhook(env: dict[str, str], *, body: bytes, signature: str) -> int:
    port = env["WEBHOOK_API_PORT"]
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-Hub-Signature-256": signature,
    }
    request = urllib.request.Request(
        f"http://localhost:{port}{_WEBHOOK_URL_PATH}", data=body, method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


# ---------------------------------------------------------------------------
# Direct Postgres queries — this scenario's own proof, per this phase's
# Tests section: "asserting chunks/embeddings/edges land in Postgres".
# ---------------------------------------------------------------------------


def _dsn(env: dict[str, str]) -> str:
    return (
        f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
        f"@localhost:{env['POSTGRES_PORT']}/{env['POSTGRES_DB']}"
    )


async def _fetch_chunks(dsn: str, *, repo_full_name: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT c.file_path, c.chunk_index, c.embedding
            FROM index_chunk c
            JOIN repo r ON r.id = c.repo_id
            WHERE r.full_name = $1
            ORDER BY c.file_path, c.chunk_index
            """,
            repo_full_name,
        )
        return cast("list[asyncpg.Record]", rows)
    finally:
        await conn.close()


async def _fetch_graph_nodes(dsn: str, *, repo_full_name: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT n.symbol_name
            FROM index_graph_node n
            JOIN repo r ON r.id = n.repo_id
            WHERE r.full_name = $1
            """,
            repo_full_name,
        )
        return cast("list[asyncpg.Record]", rows)
    finally:
        await conn.close()


async def _fetch_graph_edges(dsn: str, *, repo_full_name: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT e.target_symbol_name
            FROM index_graph_edge e
            JOIN index_graph_node n ON n.id = e.source_node_id
            JOIN repo r ON r.id = n.repo_id
            WHERE r.full_name = $1
            """,
            repo_full_name,
        )
        return cast("list[asyncpg.Record]", rows)
    finally:
        await conn.close()


def _parse_pgvector(raw: str) -> list[float]:
    """``asyncpg`` has no built-in codec for pgvector's ``vector`` column
    type (unlike the SQLAlchemy/``pgvector`` Python adapter this platform's
    own code uses) — it comes back as that type's plain text
    representation (``"[0.1,0.2,...]"``), the same round-trip shape
    ``adapter/storage/test_indexing_repository.py``'s own
    ``test_upsert_chunks_embedding_round_trips_exactly`` parses, not a
    Python list."""
    return [float(v) for v in raw.strip("[]").split(",")]


def _wait_for_chunks(
    dsn: str, *, repo_full_name: str, timeout: float = _WAIT_TIMEOUT_SECONDS
) -> list[asyncpg.Record]:
    deadline = time.monotonic() + timeout
    rows: list[asyncpg.Record] = []
    while time.monotonic() < deadline:
        rows = asyncio.run(_fetch_chunks(dsn, repo_full_name=repo_full_name))
        if rows:
            return rows
        time.sleep(1)
    raise AssertionError(f"no index_chunk rows for {repo_full_name!r} within {timeout}s")


# ---------------------------------------------------------------------------
# The scenario itself.
# ---------------------------------------------------------------------------


def test_push_webhook_indexes_pushed_commit_end_to_end(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
) -> None:
    commit_sha = "b" * 40
    body = _push_payload(commit_sha=commit_sha)

    status = _post_webhook(running_stack, body=body, signature=_sign(body))
    assert status == 200

    dsn = _dsn(running_stack)
    chunk_rows = _wait_for_chunks(dsn, repo_full_name=_OWNER_REPO)

    # US3/US4: real chunks with real, non-empty 384-dimension embedding
    # vectors (see _parse_pgvector's own docstring for why this isn't a
    # plain `len(row["embedding"])` check).
    assert {row["file_path"] for row in chunk_rows} == {"app.ts"}
    assert all(row["embedding"] is not None for row in chunk_rows)
    assert all(len(_parse_pgvector(row["embedding"])) == 384 for row in chunk_rows)

    # US5: real graph nodes/edges — both functions defined, and the real
    # caller -> helper call edge extracted.
    node_rows = asyncio.run(_fetch_graph_nodes(dsn, repo_full_name=_OWNER_REPO))
    symbols = {row["symbol_name"] for row in node_rows}
    assert "helper" in symbols
    assert "caller" in symbols

    edge_rows = asyncio.run(_fetch_graph_edges(dsn, repo_full_name=_OWNER_REPO))
    assert any(row["target_symbol_name"] == "helper" for row in edge_rows)

    # The real GitHub call chain this scenario exercises.
    calls = stub_github_server.calls  # type: ignore[attr-defined]
    joined_calls = "\n".join(calls.calls)
    assert "access_tokens" in joined_calls
    assert f"/repos/{_OWNER_REPO}/git/trees/{commit_sha}" in joined_calls
    assert f"/repos/{_OWNER_REPO}/git/blobs/{_APP_BLOB_SHA}" in joined_calls
