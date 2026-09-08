"""docs/plans/plan-deliver-context-builder.md's Phase 6 own E2E scenario —
the plan's Traceability table's "All (happy path)" row and
docs/specs/spec-context-builder.md's US1-US10 combined happy-path row: the
full Docker Compose stack (Webhook API + Worker + Postgres/pgvector, real
local embedding model), a fixture repo with a real cross-file caller, a PR
that modifies the called function, asserting the posted finding includes a
real, platform-rendered citation referencing the real caller.

**Two webhook flows, one stub GitHub server:**

1. A ``push`` webhook (indexing enabled, per
   ``tests/e2e/test_indexing_pipeline.py``'s own pattern) indexes the
   default branch at ``_INDEX_COMMIT_SHA`` — two real TypeScript files,
   ``helper.ts`` (one exported function) and ``caller.ts`` (a *different*
   file that imports and calls it) — populating ``index_chunk``/
   ``index_graph_edge`` for ``pr_number = 0`` with a real cross-file call
   edge (``caller`` -> ``helper``).
2. A ``pull_request`` ``opened`` webhook, whose diff modifies
   ``helper.ts``'s body (base = ``_INDEX_COMMIT_SHA``, head =
   ``_HEAD_SHA``), drives the real review pipeline: prefilter ->
   static_analysis -> context_builder -> intent_summary ->
   review_generation -> verification -> output.
   ``context_builder`` resolves the changed ``helper`` symbol, queries the
   default-branch graph, finds ``caller``'s real one-hop call edge, and
   assembles a non-empty ``ContextBundle`` whose first item (id ``0``) is
   that caller hit. The stub LLM Gateway (self-contained to this file, the
   same pattern ``tests/e2e/test_review_pipeline.py`` already established)
   answers with a canned finding whose own ``context_ref_ids: [0]``
   references that bundle item — proving this plan's own citation-schema
   plumbing (`_LLMFindingItem` -> `Finding.context_ref_ids` ->
   `render_citation`) survives the *entire* real stack, not just each
   phase's own isolated test.

One Compose stack for the whole module (module-scoped `running_stack`),
same reasoning as the other Phase 5/7 E2E files: bringing the stack up is
expensive; this file needs only one PR/commit-SHA combination since it is
a single happy-path scenario, not a matrix of cases sharing one stack.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

import asyncpg
import pytest

from tests.e2e.conftest import (
    base_compose_env,
    compose,
    compose_down,
    wait_for_healthy,
    wait_for_log_line,
)

STUB_GITHUB_PORT = 19082
STUB_LLM_REVIEW_PORT = 19235
_OWNER_REPO = "acme/context-builder-e2e"
_INSTALLATION_ID = 987654
_WEBHOOK_URL_PATH = "/webhooks/github"
_WEBHOOK_SECRET = "e2e-test-webhook-secret"
_WAIT_TIMEOUT_SECONDS = 120.0
_PR_NUMBER = 7

_INDEX_COMMIT_SHA = "b" * 40  # also this PR's own base_sha
_HEAD_SHA = "c" * 40

_HELPER_BASE_BLOB_SHA = "1" * 40
_HELPER_HEAD_BLOB_SHA = "2" * 40
_CALLER_BLOB_SHA = "3" * 40

# Two real files: `caller.ts` (unchanged by the PR) calls `helper.ts`'s
# exported function — a real *cross-file* caller, per this module's own
# docstring and the plan's own E2E requirement ("fixture repo with a real
# cross-file caller"), not the same-file case
# `tests/e2e/test_indexing_pipeline.py`'s own fixture already covers.
#
# `helper.ts` carries a real leading comment block (rather than a bare
# one-liner) so this PR's own diff carries enough bytes for
# `review.context_bundle.assemble_context_bundle`'s proportional-to-diff
# budget (Non-Functional Requirements — "the context bundle's byte budget
# ... equals that job's own bare-diff byte size") to actually admit the one
# real caller-hit item this scenario exists to prove reaches a posted
# citation — a bare single-line diff's own byte size is smaller than even
# one serialized context item, which would otherwise truncate the whole
# bundle to empty before this scenario ever got to exercise citation
# rendering at all.
_HELPER_TS_BASE = (
    b"// helper.ts\n"
    b"//\n"
    b"// A small utility module used by other files in this repository. Kept\n"
    b"// intentionally documented (rather than a bare one-liner) so a realistic\n"
    b"// PR diff around a single-line change still carries a realistic amount of\n"
    b"// surrounding context, the same shape a real-world diff would have.\n"
    b"//\n"
    b"export function helper(): number {\n"
    b"  return 1;\n"
    b"}\n"
)
_HELPER_TS_HEAD = _HELPER_TS_BASE.replace(b"return 1;", b"return 2;")
_CALLER_TS = (
    b'import { helper } from "./helper";\n'
    b"\n"
    b"export function caller(): number {\n"
    b"  return helper();\n"
    b"}\n"
)

_HELPER_PATCH = (
    "@@ -1,10 +1,10 @@\n"
    " // helper.ts\n"
    " //\n"
    " // A small utility module used by other files in this repository. Kept\n"
    " // intentionally documented (rather than a bare one-liner) so a realistic\n"
    " // PR diff around a single-line change still carries a realistic amount of\n"
    " // surrounding context, the same shape a real-world diff would have.\n"
    " //\n"
    " export function helper(): number {\n"
    "-  return 1;\n"
    "+  return 2;\n"
    " }\n"
)


# ---------------------------------------------------------------------------
# Stub GitHub server — answers both the push-indexing call chain
# (installation-token exchange, Git Trees/Blobs) and the PR-review call
# chain (PR object, Files endpoint, a *second* Git Trees/Blobs pair at the
# PR's own base_sha per Assumption 1, the two posting calls), all against
# one owner/repo, keyed by commit SHA so pre-change (`_INDEX_COMMIT_SHA`)
# and post-change (`_HEAD_SHA`) content genuinely differ.
# ---------------------------------------------------------------------------


@dataclass
class _CallLog:
    calls: list[str] = field(default_factory=list)
    review_bodies: list[dict[str, object]] = field(default_factory=list)
    check_run_bodies: list[dict[str, object]] = field(default_factory=list)


def _tree_entry(path: str, blob_sha: str, content: bytes) -> dict[str, object]:
    return {"type": "blob", "path": path, "mode": "100644", "sha": blob_sha, "size": len(content)}


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

    def _read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        result: dict[str, object] = json.loads(raw)
        return result

    def do_POST(self) -> None:
        path = self.path
        self._calls().calls.append(f"POST {path}")

        if re.fullmatch(r"/app/installations/\d+/access_tokens", path):
            self._send_json(
                201, {"token": "ghs_e2e_context_token", "expires_at": "2999-01-01T00:00:00Z"}
            )
            return

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+/reviews", path):
            self._calls().review_bodies.append(self._read_json_body())
            self._send_json(200, {"id": 1})
            return

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/check-runs", path):
            self._calls().check_run_bodies.append(self._read_json_body())
            self._send_json(201, {"id": 1})
            return

        self._send_json(404, {"message": "not found (stub)"})

    def do_GET(self) -> None:
        path_only = self.path.split("?", 1)[0]
        self._calls().calls.append(f"GET {self.path}")

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+", path_only):
            self._send_json(200, {"changed_files": 1})
            return

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+/files", path_only):
            self._send_json(
                200,
                [{"filename": "helper.ts", "status": "modified", "patch": _HELPER_PATCH}],
            )
            return

        match = re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/git/trees/(\w+)", path_only)
        if match:
            sha = match.group(1)
            if sha == _HEAD_SHA:
                tree = [
                    _tree_entry("helper.ts", _HELPER_HEAD_BLOB_SHA, _HELPER_TS_HEAD),
                    _tree_entry("caller.ts", _CALLER_BLOB_SHA, _CALLER_TS),
                ]
            else:
                # `_INDEX_COMMIT_SHA` — both the push-indexed commit and
                # this PR's own base_sha (kept equal deliberately: the
                # simplest fixture that still gives the blast-radius query
                # something real to find under `pr_number = 0`).
                tree = [
                    _tree_entry("helper.ts", _HELPER_BASE_BLOB_SHA, _HELPER_TS_BASE),
                    _tree_entry("caller.ts", _CALLER_BLOB_SHA, _CALLER_TS),
                ]
            self._send_json(200, {"tree": tree, "truncated": False})
            return

        match = re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/git/blobs/(\w+)", path_only)
        if match:
            blobs = {
                _HELPER_BASE_BLOB_SHA: _HELPER_TS_BASE,
                _HELPER_HEAD_BLOB_SHA: _HELPER_TS_HEAD,
                _CALLER_BLOB_SHA: _CALLER_TS,
            }
            content = blobs.get(match.group(1), b"")
            self._send_json(
                200, {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"}
            )
            return

        self._send_json(404, {"message": "not found (stub)"})


@pytest.fixture(scope="module")
def stub_github_server() -> Iterator[HTTPServer]:
    server = HTTPServer(("0.0.0.0", STUB_GITHUB_PORT), _StubGitHubHandler)
    server.calls = _CallLog()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Stub LLM Gateway — self-contained to this file (mirrors
# tests/e2e/test_review_pipeline.py's own `_StubLLMHandler`). A single review
# job now makes up to three calls to this endpoint, in a fixed order —
# `intent_summary`, then `review_generation`, then `verification` — each
# expecting a differently-shaped response (plain text; `{"findings": [...]}`;
# `{"verdicts": [...]}`, respectively). This handler routes by the
# per-server `call_count` position, same as `test_review_pipeline.py`'s own
# `_StubLLMHandler`, rather than answering every call with the same
# `{"findings": [...]}` body regardless of which step made it — the latter
# broke `verification`'s own response parsing once this plan added it as a
# third call on the same endpoint. `review_generation`'s canned finding
# (call 2) still carries `context_ref_ids: [0]` naming the context bundle's
# own first item — the real caller hit this scenario's whole point is to
# prove reaches a posted citation — and `verification`'s canned verdict
# (call 3) passes that one finding on all four criteria so it survives to
# the posted review.
# ---------------------------------------------------------------------------


class _StubLLMHandler(BaseHTTPRequestHandler):
    _INTENT_SUMMARY_CONTENT = "This PR changes a function's return value."
    _FINDINGS_CONTENT = json.dumps(
        {
            "findings": [
                {
                    "file": "helper.ts",
                    "line": 9,
                    "severity": "medium",
                    "message": "Changing this return value affects at least one known caller.",
                    "context_ref_ids": [0],
                }
            ]
        }
    )
    _VERDICT_VERIFIED_CONTENT = json.dumps(
        {
            "verdicts": [
                {"index": 0, "valid": True, "actionable": True, "specific": True, "valuable": True}
            ]
        }
    )

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _content_for_call(self, call_number: int) -> str:
        """1 = `intent_summary`, 2 = `review_generation`, 3 = `verification`."""
        if call_number <= 1:
            return self._INTENT_SUMMARY_CONTENT
        if call_number == 2:
            return self._FINDINGS_CONTENT
        return self._VERDICT_VERIFIED_CONTENT

    def do_POST(self) -> None:
        call_number = getattr(self.server, "call_count", 0) + 1
        self.server.call_count = call_number  # type: ignore[attr-defined]

        body = json.dumps(
            {
                "id": "chatcmpl-e2e-context-builder-stub",
                "object": "chat.completion",
                "created": 0,
                "model": "local-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": self._content_for_call(call_number),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def stub_llm_review_server() -> Iterator[HTTPServer]:
    server = HTTPServer(("0.0.0.0", STUB_LLM_REVIEW_PORT), _StubLLMHandler)
    server.call_count = 0  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Compose stack.
# ---------------------------------------------------------------------------


def _e2e_env() -> dict[str, str]:
    return base_compose_env(
        REVIEW_WORKER_LLM_ENDPOINT=f"http://host.docker.internal:{STUB_LLM_REVIEW_PORT}/v1",
        REVIEW_WORKER_GITHUB_API_BASE_URL=f"http://host.docker.internal:{STUB_GITHUB_PORT}",
        REVIEW_WORKER_POLL_INTERVAL_SECONDS="0.2",
        # Indexing must be enabled for the `push` webhook below to enqueue
        # an INDEX job at all — same affirmative opt-in
        # `tests/e2e/test_indexing_pipeline.py` already exercises.
        # `context_builder_enabled` is left at its own default (`True`,
        # per the spec's decided kill-switch NFR) — this scenario is
        # exactly the "does it actually work, on by default" happy path.
        REVIEW_WEBHOOK_API_INDEXING_ENABLED="true",
    )


@pytest.fixture(scope="module")
def running_stack(
    stub_github_server: HTTPServer, stub_llm_review_server: HTTPServer
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


def _push_payload() -> bytes:
    return json.dumps(
        {
            "installation": {"id": _INSTALLATION_ID},
            "repository": {"full_name": _OWNER_REPO, "default_branch": "main"},
            "ref": "refs/heads/main",
            "before": "0" * 40,
            "after": _INDEX_COMMIT_SHA,
            "commits": [{"added": ["helper.ts", "caller.ts"], "removed": [], "modified": []}],
            "forced": False,
        }
    ).encode()


def _pull_request_payload() -> bytes:
    return json.dumps(
        {
            "installation": {"id": _INSTALLATION_ID},
            "repository": {"full_name": _OWNER_REPO},
            "action": "opened",
            "pull_request": {
                "number": _PR_NUMBER,
                "head": {"sha": _HEAD_SHA},
                "base": {"sha": _INDEX_COMMIT_SHA},
            },
        }
    ).encode()


def _post_webhook(env: dict[str, str], *, body: bytes, event_type: str) -> int:
    port = env["WEBHOOK_API_PORT"]
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event_type,
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-Hub-Signature-256": _sign(body),
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
# Direct Postgres check that indexing actually landed, before the PR event
# is sent — mirrors tests/e2e/test_indexing_pipeline.py's own wait helper,
# so the blast-radius query has real `pr_number = 0` data to find rather
# than racing the indexing job.
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
            SELECT c.file_path
            FROM index_chunk c
            JOIN repo r ON r.id = c.repo_id
            WHERE r.full_name = $1
            """,
            repo_full_name,
        )
        return list(rows)
    finally:
        await conn.close()


def _wait_for_indexed(
    dsn: str, *, repo_full_name: str, timeout: float = _WAIT_TIMEOUT_SECONDS
) -> None:
    deadline = time.monotonic() + timeout
    rows: list[asyncpg.Record] = []
    while time.monotonic() < deadline:
        rows = asyncio.run(_fetch_chunks(dsn, repo_full_name=repo_full_name))
        paths = {row["file_path"] for row in rows}
        if {"helper.ts", "caller.ts"} <= paths:
            return
        time.sleep(1)
    raise AssertionError(
        f"index_chunk never contained both fixture files for {repo_full_name!r} within "
        f"{timeout}s (saw: {sorted({row['file_path'] for row in rows})})"
    )


def _wait_until_review_posted(calls: _CallLog, *, timeout: float = _WAIT_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if calls.check_run_bodies:
            return
        time.sleep(0.5)
    raise AssertionError(f"no check-run posted within {timeout}s (calls so far: {calls.calls!r})")


# ---------------------------------------------------------------------------
# The scenario itself.
# ---------------------------------------------------------------------------


def test_pr_modifying_a_called_function_posts_a_finding_with_a_real_citation(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
    stub_llm_review_server: HTTPServer,
) -> None:
    # 1. Index the default branch — populates `index_chunk`/
    #    `index_graph_edge` for `pr_number = 0` with a real
    #    `caller` -> `helper` call edge across two files.
    push_status = _post_webhook(running_stack, body=_push_payload(), event_type="push")
    assert push_status == 200

    dsn = _dsn(running_stack)
    _wait_for_indexed(dsn, repo_full_name=_OWNER_REPO)

    # Resets the stub LLM Gateway's call-position counter to 0 — the
    # Worker's own one-time startup LLM-reachability check
    # (`llmgateway/startup_check.py`) already fired once when `running_stack`
    # first came up, consuming call 1 before this test's own PR-triggered
    # job makes any call; without this reset, `_StubLLMHandler`'s
    # call-position routing (1 = `intent_summary`, 2 = `review_generation`,
    # 3 = `verification`) would be off by one for the job below. Mirrors
    # `tests/e2e/test_review_pipeline.py`'s own `_reset_llm_scenario`.
    stub_llm_review_server.call_count = 0  # type: ignore[attr-defined]

    # 2. A PR that changes `helper`'s body — the review pipeline's own
    #    context_builder step should find `caller`'s real one-hop edge and
    #    hand it to review_generation; the stub LLM Gateway's canned finding then
    #    cites it back via `context_ref_ids: [0]`.
    pr_status = _post_webhook(
        running_stack, body=_pull_request_payload(), event_type="pull_request"
    )
    assert pr_status == 200

    calls: _CallLog = stub_github_server.calls  # type: ignore[attr-defined]
    _wait_until_review_posted(calls)

    assert len(calls.review_bodies) == 1
    posted_body = json.dumps(calls.review_bodies[0])

    # The real posted finding text (escaped per `escape_markdown`, hence
    # the loose substring checks rather than an exact-string match).
    assert "Changing this return value affects" in posted_body
    # The platform-rendered citation itself — the real caller's file path
    # and symbol name, sourced from the real assembled `ContextBundle`,
    # never text the LLM composed.
    assert "caller" in posted_body
    assert "Context" in posted_body

    assert calls.check_run_bodies[-1]["conclusion"] == "neutral"
