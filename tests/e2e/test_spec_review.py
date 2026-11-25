"""
E2E scenario for docs/plans/plan-deliver-smell-and-spec-review.md Phase 4 (Unit D, US-S9 plus the
US-S4/S5/S6/S7/S10/S12 happy path and failure mode that matter), over the real Docker Compose stack
(Webhook API + Worker + Postgres) with a stub GitHub and a stub LLM.

**One stack, three PR reviews:**

1. PR #1, head ref ``feature/billing-retry``: the repo (at the head commit) holds
   ``docs/specs/spec-billing-retry.md``. The spec is resolved from the repo tree, one extra LLM call
   reports a ``missing`` requirement, verification keeps it, and the posted review body carries it
   under "Findings not attributable to a specific line" (``line=None``, US-S12).
2. PR #2, head ref ``chore/tidy``: no spec matches. No spec LLM call is made and the review body has
   no spec finding (output unchanged).
3. PR #3, head ref ``feature/timeout-retry``: a spec matches but the stub LLM stalls the spec call
   past ``spec_review``'s internal deadline. The step degrades (US-S10): the ordinary review is
   still posted, and the job does not fail.

The stack runs with a short ``spec_review`` internal/step timeout pair (2 s / 10 s) so scenario 3
does not wait the production 45 s; the internal deadline stays strictly below the step timeout, as
``WorkerSettings`` requires.
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

STUB_GITHUB_PORT = 19084
STUB_LLM_PORT = 19238
_OWNER_REPO = "acme/spec-review-e2e"
_INSTALLATION_ID = 987656
_WEBHOOK_SECRET = "e2e-test-webhook-secret"
_WAIT_TIMEOUT_SECONDS = 120.0
_SPEC_STALL_SECONDS = 6.0  # longer than the 2 s internal deadline below

_BILLING_SPEC_PATH = "docs/specs/spec-billing-retry.md"
_BILLING_SPEC = b"# Billing retry\n\nEvery retry must carry an idempotency key.\n"
_TIMEOUT_SPEC_PATH = "docs/specs/spec-timeout-retry.md"
_TIMEOUT_SPEC = b"# Timeout retry\n\nRetries must back off exponentially.\n"
_BLOBS = {"b1" * 20: _BILLING_SPEC, "b2" * 20: _TIMEOUT_SPEC}

_PATCH = "@@ -1,3 +1,3 @@\n export function helper(): number {\n-  return 1;\n+  return 2;\n }\n"
_SPEC_FINDING_MESSAGE = "Retries are sent without a generated idempotency token"


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
        raw = self.rfile.read(length) if length else b"{}"
        result: dict[str, object] = json.loads(raw)
        return result

    def do_POST(self) -> None:
        path = self.path
        if re.fullmatch(r"/app/installations/\d+/access_tokens", path):
            self._send_json(
                201, {"token": f"ghs_{uuid.uuid4().hex}", "expires_at": "2999-01-01T00:00:00Z"}
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
            self._send_json(200, {"changed_files": 1})
        elif re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+/files", path):
            self._send_json(200, [{"filename": "helper.ts", "status": "modified", "patch": _PATCH}])
        elif re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/git/trees/\w+", path):
            tree = [
                {"type": "blob", "path": "helper.ts", "mode": "100644", "sha": "c3" * 20, "size": 9}
            ]
            for spec_path, sha, blob in (
                (_BILLING_SPEC_PATH, "b1" * 20, _BILLING_SPEC),
                (_TIMEOUT_SPEC_PATH, "b2" * 20, _TIMEOUT_SPEC),
            ):
                tree.append(
                    {
                        "type": "blob",
                        "path": spec_path,
                        "mode": "100644",
                        "sha": sha,
                        "size": len(blob),
                    }
                )
            self._send_json(200, {"tree": tree, "truncated": False})
        elif match := re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/git/blobs/(\w+)", path):
            content = _BLOBS.get(match.group(1), b"")
            self._send_json(
                200, {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"}
            )
        else:
            self._send_json(404, {"message": "not found (stub)"})


class _StubLLMHandler(BaseHTTPRequestHandler):
    """Routes by what the request is for (not by position: spec_review is conditional)."""

    def log_message(self, *_args: object) -> None:
        pass

    @staticmethod
    def _content_for(request_body: str) -> str:
        if "spec-compliance reviewer" in request_body:
            return json.dumps(
                {
                    "findings": [
                        {
                            "kind": "missing",
                            "file": None,
                            "line": None,
                            "severity": "medium",
                            "message": _SPEC_FINDING_MESSAGE,
                            "spec_quote": "Every retry must carry an idempotency key.",
                        }
                    ]
                }
            )
        if '"verdicts"' in request_body:
            verdict = {
                "valid": True,
                "actionable": True,
                "specific": True,
                "valuable": True,
            }
            # Verdicts for every candidate index (at most two in this scenario).
            return json.dumps({"verdicts": [{"index": i, **verdict} for i in range(2)]})
        if "ONLY that summary" in request_body:
            return "This PR changes a function's return value."
        return json.dumps(
            {
                "findings": [
                    {
                        "file": "helper.ts",
                        "line": 2,
                        "severity": "medium",
                        "message": "Changing this return value may break billing retries.",
                    }
                ]
            }
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request_body = self.rfile.read(length).decode("utf-8", "replace")
        is_spec_call = "spec-compliance reviewer" in request_body
        if is_spec_call:
            self.server.spec_calls.append(request_body)  # type: ignore[attr-defined]
            if "Timeout retry" in request_body:
                time.sleep(_SPEC_STALL_SECONDS)
        body = json.dumps(
            {
                "id": "chatcmpl-e2e-spec-stub",
                "object": "chat.completion",
                "created": 0,
                "model": "local-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": self._content_for(request_body),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the worker's internal deadline already abandoned this call (scenario 3)


def _serve(server: HTTPServer) -> Iterator[HTTPServer]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
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
def stub_llm_server_spec() -> Iterator[HTTPServer]:
    server = ThreadingHTTPServer(("0.0.0.0", STUB_LLM_PORT), _StubLLMHandler)
    server.spec_calls = []  # type: ignore[attr-defined]
    yield from _serve(server)


@pytest.fixture(scope="module")
def running_stack(
    stub_github_server: HTTPServer, stub_llm_server_spec: HTTPServer
) -> Iterator[dict[str, str]]:
    env = base_compose_env(
        REVIEW_WORKER_LLM_ENDPOINT=f"http://host.docker.internal:{STUB_LLM_PORT}/v1",
        REVIEW_WORKER_GITHUB_API_BASE_URL=f"http://host.docker.internal:{STUB_GITHUB_PORT}",
        REVIEW_WORKER_POLL_INTERVAL_SECONDS="0.2",
        REVIEW_WORKER_SPEC_REVIEW_STEP_TIMEOUT_SECONDS="10",
        REVIEW_WORKER_SPEC_REVIEW_INTERNAL_TIMEOUT_SECONDS="2",
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


def _pull_request_payload(*, number: int, head_ref: str) -> bytes:
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
                    "ref": head_ref,
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


def _review_and_wait(env: dict[str, str], github: HTTPServer, *, number: int, head_ref: str) -> str:
    """Posts one PR webhook and returns the review body posted for it."""
    calls: _CallLog = github.calls  # type: ignore[attr-defined]
    already = len(calls.check_run_bodies)
    status = _post_pull_request(env, _pull_request_payload(number=number, head_ref=head_ref))
    assert status == 200
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if len(calls.check_run_bodies) > already:
            return json.dumps(calls.review_bodies[-1])
        time.sleep(0.5)
    raise AssertionError(f"no check-run posted for PR #{number} within {_WAIT_TIMEOUT_SECONDS}s")


def test_spec_finding_posted_when_spec_matches_and_review_survives_no_spec_and_stalled_llm(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
    stub_llm_server_spec: HTTPServer,
) -> None:
    env = running_stack
    spec_calls: list[str] = stub_llm_server_spec.spec_calls  # type: ignore[attr-defined]

    # 1. Branch matches a repo spec: the spec finding is verified and posted in the review body.
    matched = _review_and_wait(env, stub_github_server, number=1, head_ref="feature/billing-retry")
    assert len(spec_calls) == 1
    assert "Every retry must carry an idempotency key." in spec_calls[0]
    assert "Findings not attributable to a specific line" in matched
    assert _SPEC_FINDING_MESSAGE in matched
    assert "Changing this return value" in matched

    # 2. No spec matches: no spec LLM call, no spec finding, ordinary review.
    unmatched = _review_and_wait(env, stub_github_server, number=2, head_ref="chore/tidy")
    assert len(spec_calls) == 1
    assert _SPEC_FINDING_MESSAGE not in unmatched
    assert "Changing this return value" in unmatched

    # 3. The spec call stalls past the internal deadline: the step degrades, the review posts.
    degraded = _review_and_wait(env, stub_github_server, number=3, head_ref="feature/timeout-retry")
    assert len(spec_calls) == 2
    assert _SPEC_FINDING_MESSAGE not in degraded
    assert "Changing this return value" in degraded
    calls: _CallLog = stub_github_server.calls  # type: ignore[attr-defined]
    assert all(body.get("conclusion") != "failure" for body in calls.check_run_bodies)
