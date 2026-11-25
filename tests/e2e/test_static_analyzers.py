"""
E2E scenario for docs/plans/plan-deliver-prompt-improvements-and-classifier.md Phase 4 (RQ8).

One PR touches a markdown doc with a broken anchor link and a SQL migration with a non-concurrent
``CREATE INDEX``, over the real Docker Compose stack (Webhook API + Worker + Postgres) with a stub
GitHub and a stub LLM. The two analyzer flags are switched on (they default off). The stub LLM
reports no findings of its own, so both posted findings can only have come from the deterministic
analyzers, and the stub records that no verification call was made (analyzer findings bypass it).
"""

from __future__ import annotations

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
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import pytest

from tests.e2e.conftest import (
    base_compose_env,
    compose,
    compose_down,
    wait_for_healthy,
    wait_for_log_line,
)

STUB_GITHUB_PORT = 19085
STUB_LLM_PORT = 19239
_OWNER_REPO = "acme/analyzers-e2e"
_INSTALLATION_ID = 987657
_WEBHOOK_SECRET = "e2e-test-webhook-secret"
_WAIT_TIMEOUT_SECONDS = 120.0

_DOC_PATH = "docs/guide.md"
_DOC = b"# Guide\n\nSee [the setup](#no-such-section) first.\n"
_SQL_PATH = "migrations/001_add_index.sql"
_SQL = b"CREATE INDEX idx_card ON canvas_connections (card_id);\n"
_FILES = {_DOC_PATH: ("d1" * 20, _DOC), _SQL_PATH: ("d2" * 20, _SQL)}
_BLOBS = dict(_FILES.values())


def _patch(content: bytes) -> str:
    lines = content.decode().splitlines()
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)


@dataclass
class _CallLog:
    review_bodies: list[dict[str, object]] = field(default_factory=list)
    check_run_bodies: list[dict[str, object]] = field(default_factory=list)


class _StubGitHubHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
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
        result: dict[str, object] = json.loads(self.rfile.read(length) if length else b"{}")
        return result

    def do_POST(self) -> None:
        path = self.path
        if re.fullmatch(r"/app/installations/\d+/access_tokens", path):
            self._send_json(
                201, {"token": "ghs_e2e_analyzer_token", "expires_at": "2999-01-01T00:00:00Z"}
            )
        elif re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+/reviews", path):
            self._calls().review_bodies.append(self._read_json_body())
            self._send_json(200, {"id": 1})
        elif re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/check-runs", path):
            self._calls().check_run_bodies.append(self._read_json_body())
            self._send_json(201, {"id": 1})
        else:
            self._send_json(404, {"message": "not found (stub)"})

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+", path):
            self._send_json(200, {"changed_files": len(_FILES)})
        elif re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+/files", path):
            self._send_json(
                200,
                [
                    {"filename": name, "status": "added", "patch": _patch(content)}
                    for name, (_, content) in _FILES.items()
                ],
            )
        elif re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/git/trees/\w+", path):
            tree = [
                {"type": "blob", "path": n, "mode": "100644", "sha": sha, "size": len(content)}
                for n, (sha, content) in _FILES.items()
            ]
            self._send_json(200, {"tree": tree, "truncated": False})
        elif match := re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/git/blobs/(\w+)", path):
            content = _BLOBS.get(match.group(1), b"")
            self._send_json(
                200, {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"}
            )
        else:
            self._send_json(404, {"message": "not found (stub)"})


class _StubLLMHandler(BaseHTTPRequestHandler):
    """Summary text, then an empty findings list; records every request body."""

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request_body = self.rfile.read(length).decode("utf-8", "replace")
        self.server.prompts.append(request_body)  # type: ignore[attr-defined]
        content = (
            "This PR adds a guide and an index migration."
            if "ONLY that summary" in request_body
            else json.dumps({"findings": []})
        )
        body = json.dumps(
            {
                "id": "chatcmpl-e2e-analyzers-stub",
                "object": "chat.completion",
                "created": 0,
                "model": "local-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
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


def _serve(server: HTTPServer) -> Iterator[HTTPServer]:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def stub_github_server() -> Iterator[HTTPServer]:
    server = HTTPServer(("0.0.0.0", STUB_GITHUB_PORT), _StubGitHubHandler)
    server.calls = _CallLog()  # type: ignore[attr-defined]
    yield from _serve(server)


@pytest.fixture(scope="module")
def stub_llm_server() -> Iterator[HTTPServer]:
    server = ThreadingHTTPServer(("0.0.0.0", STUB_LLM_PORT), _StubLLMHandler)
    server.prompts = []  # type: ignore[attr-defined]
    yield from _serve(server)


@pytest.fixture(scope="module")
def running_stack(
    stub_github_server: HTTPServer, stub_llm_server: HTTPServer
) -> Iterator[dict[str, str]]:
    env = base_compose_env(
        REVIEW_WORKER_LLM_ENDPOINT=f"http://host.docker.internal:{STUB_LLM_PORT}/v1",
        REVIEW_WORKER_GITHUB_API_BASE_URL=f"http://host.docker.internal:{STUB_GITHUB_PORT}",
        REVIEW_WORKER_POLL_INTERVAL_SECONDS="0.2",
        REVIEW_WORKER_STATIC_ANALYSIS_MARKDOWN_LINKS_ENABLED="true",
        REVIEW_WORKER_STATIC_ANALYSIS_SQL_MIGRATIONS_ENABLED="true",
    )
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
        print(compose("logs", "--no-color", env=env).stdout)
        compose_down(env)


def _pull_request_payload(*, number: int) -> bytes:
    return json.dumps(
        {
            "installation": {"id": _INSTALLATION_ID},
            "repository": {"full_name": _OWNER_REPO, "private": True},
            "action": "opened",
            "pull_request": {
                "number": number,
                "title": f"PR {number}",
                "head": {
                    "sha": f"{number:040x}",
                    "ref": "feature/docs-and-index",
                    "repo": {"full_name": _OWNER_REPO, "fork": False},
                },
                "base": {"sha": "b" * 40, "repo": {"full_name": _OWNER_REPO}},
            },
        }
    ).encode()


def _post_pull_request(env: dict[str, str], body: bytes) -> int:
    digest = hmac.new(_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        f"http://localhost:{env['WEBHOOK_API_PORT']}/webhooks/github",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": f"sha256={digest}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def test_doc_and_migration_findings_are_posted_with_no_llm_call_for_them(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
    stub_llm_server: HTTPServer,
) -> None:
    calls: _CallLog = stub_github_server.calls  # type: ignore[attr-defined]
    prompts: list[str] = stub_llm_server.prompts  # type: ignore[attr-defined]
    assert _post_pull_request(running_stack, _pull_request_payload(number=1)) == 200
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    while not calls.check_run_bodies and time.monotonic() < deadline:
        time.sleep(0.5)
    assert calls.check_run_bodies, f"no check-run posted within {_WAIT_TIMEOUT_SECONDS}s"

    review = json.dumps(calls.review_bodies[-1])
    assert "Broken link" in review
    assert "CREATE INDEX without CONCURRENTLY" in review
    # The LLM reported nothing, and analyzer findings skip verification: no verdict request.
    assert not any('"verdicts"' in prompt for prompt in prompts)
    assert not any("CREATE INDEX without CONCURRENTLY" in prompt for prompt in prompts)
