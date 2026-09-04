"""Phase 7 E2E: the roadmap's literal Phase 1 exit criteria — "a real PR
against a real repo gets a real (if unsophisticated) review posted by the
platform" — driven over real HTTP against the running Docker Compose stack
(Webhook API + Worker + Postgres), a synthetic signed GitHub webhook payload
in, a stub GitHub server standing in for `api.github.com` out, per
docs/rules/testing.md's top tier ("no shortcuts that bypass the Webhook
API's actual entrypoint") and the plan's Phase 7 Tests section.

`stub_github_server` extends the `stub_llm_server`/Compose-driving pattern
`tests/e2e/conftest.py` already established in Phase 0 — bound on every
interface (reachable via Docker's `host-gateway` bridge, same as
`stub_llm_server`), serving the whole GitHub call chain this round's
diff-fetch/posting design introduced: installation-token exchange, the PR
object (`changed_files`), the paginated Files-endpoint, the Git Trees API,
the Git Blobs API, and the two posting calls (create PR review, create
check-run) — `REVIEW_WORKER_GITHUB_API_BASE_URL` points the Worker's every
`adapter/github` call at it, since `auth.py`/`diff.py`/`posting.py` all
share that one setting. A second, small stub stands in for the LLM Gateway
too (self-contained to this file, on its own port, rather than reusing
`tests/e2e/conftest.py`'s package-scoped `stub_llm_server` — that one's
own behavior is fixed, shared with `test_compose_stack.py`, and this file
needs per-test-configurable LLM outcomes, which would mean either mutating
shared package fixture state other tests don't expect or a second,
equivalent stub anyway).

One Compose stack is brought up once for the whole module (`running_stack`,
module-scoped) rather than per test function — `docker compose up --build`
plus its own health-check waits is expensive; each test function instead
selects a distinct PR number/head SHA (never reused, so idempotency/dedupe
never interferes across cases) and reconfigures the two stub servers'
shared, mutable scenario state before POSTing its own synthetic webhook.

Per testing.md's explicit E2E failure-mode list ("analyzer crash, LLM
timeout, no findings, malformed webhook signature"): this file covers LLM
timeout, no findings, and malformed webhook signature directly. The
"analyzer crash" case is **not** reproduced at this tier — deterministically
crashing one specific real analyzer *binary* (not a fake standing in for
one) via black-box, network-served fixture content alone, without also
disabling the other analyzer (a different property, US-F4, not US9), isn't
achievable without binary-specific knowledge this plan doesn't otherwise
need; the property itself (one analyzer's failure stays isolated, the
review still completes using the other's and the LLM's findings) is already
proven twice at Integration tier — `review/test_static_analysis_step.py`'s
own real-binary case (Phase 5) and `review/test_review_job.py`'s real,
fully-wired-registry case (Phase 7, this same plan) — which
`docs/adr/0005`'s own "the closest the framework's own stories get to
boundary-crossing coverage" framing already treats as sufficient without
every property also needing its own E2E repetition. Also covered: the
E2E-level proof of Phase 7's own US8-scope fix (a fatal failure *before*
llm-review, not only the llm-review case US8's own PRD wording names,
still produces a GitHub-visible failure-conclusion check-run).
"""

from __future__ import annotations

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
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    base_compose_env,
    compose,
    compose_down,
    wait_for_healthy,
    wait_for_log_line,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
STUB_GITHUB_PORT = 19080
STUB_LLM_REVIEW_PORT = 19234
_OWNER_REPO = "acme/widgets"
_INSTALLATION_ID = 123456
_WEBHOOK_URL_PATH = "/webhooks/github"
_WEBHOOK_SECRET = "e2e-test-webhook-secret"
_WAIT_TIMEOUT_SECONDS = 60.0

_SECRET_FILE_CONTENT = b'github_token = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"\n'


# ---------------------------------------------------------------------------
# Stub GitHub server
# ---------------------------------------------------------------------------


@dataclass
class _GitHubScenario:
    files: list[dict[str, object]]
    tree: list[dict[str, object]]
    blobs: dict[str, bytes]
    changed_files_count: int | None = None
    files_endpoint_status: int = 200


@dataclass
class _CallLog:
    calls: list[str] = field(default_factory=list)
    review_bodies: list[dict[str, object]] = field(default_factory=list)
    check_run_bodies: list[dict[str, object]] = field(default_factory=list)


def _single_file_scenario(
    *, path: str = "app.py", content: bytes = _SECRET_FILE_CONTENT
) -> _GitHubScenario:
    blob_sha = "f" * 40
    return _GitHubScenario(
        files=[{"filename": path, "status": "added", "patch": "@@ -0,0 +1 @@\n+planted"}],
        tree=[{"path": path, "mode": "100644", "sha": blob_sha, "size": len(content)}],
        blobs={blob_sha: content},
    )


class _StubGitHubHandler(BaseHTTPRequestHandler):
    """Answers the whole GitHub call chain this plan's diff-fetch/posting
    design makes, per this module's own docstring. Scenario/call-log state
    lives on `self.server` (a plain `HTTPServer`, free to carry extra
    attributes) rather than the handler class itself, since a fresh handler
    instance is constructed per request.
    """

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _scenario(self) -> _GitHubScenario:
        scenario = getattr(self.server, "scenario", None)
        assert isinstance(scenario, _GitHubScenario)
        return scenario

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
                201, {"token": "ghs_e2e_test_token", "expires_at": "2999-01-01T00:00:00Z"}
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
        scenario = self._scenario()

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+", path_only):
            count = (
                scenario.changed_files_count
                if scenario.changed_files_count is not None
                else len(scenario.files)
            )
            self._send_json(200, {"changed_files": count})
            return

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+/files", path_only):
            self._send_json(scenario.files_endpoint_status, scenario.files)
            return

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/git/trees/\w+", path_only):
            self._send_json(200, {"tree": scenario.tree, "truncated": False})
            return

        match = re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/git/blobs/(\w+)", path_only)
        if match:
            content = scenario.blobs.get(match.group(1), b"")
            self._send_json(
                200, {"content": b64encode(content).decode("ascii"), "encoding": "base64"}
            )
            return

        self._send_json(404, {"message": "not found (stub)"})


@pytest.fixture(scope="module")
def stub_github_server() -> Iterator[HTTPServer]:
    server = HTTPServer(("0.0.0.0", STUB_GITHUB_PORT), _StubGitHubHandler)
    server.scenario = _single_file_scenario()  # type: ignore[attr-defined]
    server.calls = _CallLog()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _reset_github_scenario(server: HTTPServer, scenario: _GitHubScenario) -> None:
    server.scenario = scenario  # type: ignore[attr-defined]
    server.calls = _CallLog()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Stub LLM Gateway server — self-contained to this file (see module docstring)
# ---------------------------------------------------------------------------


class _StubLLMHandler(BaseHTTPRequestHandler):
    """Answers `POST /v1/chat/completions` (and, for the Worker's own
    startup reachability check, whatever else LiteLLM sends) with either a
    well-formed, empty-findings completion (the default — every case except
    the LLM-timeout one below needs llm-review to *succeed*, since only
    that one case is this stub's own reason for existing at a per-test-
    configurable granularity) or a `500` (the "llm_failure" mode), per
    `self.server.mode`.
    """

    _SUCCESS_BODY = json.dumps(
        {
            "id": "chatcmpl-e2e-review-stub",
            "object": "chat.completion",
            "created": 0,
            "model": "local-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": '{"findings": []}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    ).encode()

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        mode = getattr(self.server, "mode", "success")
        if mode == "llm_failure":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "simulated failure"}')
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self._SUCCESS_BODY)


@pytest.fixture(scope="module")
def stub_llm_review_server() -> Iterator[HTTPServer]:
    server = HTTPServer(("0.0.0.0", STUB_LLM_REVIEW_PORT), _StubLLMHandler)
    server.mode = "success"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Compose stack — one per module, per this file's own docstring
# ---------------------------------------------------------------------------


def _e2e_env() -> dict[str, str]:
    return base_compose_env(
        REVIEW_WORKER_LLM_ENDPOINT=f"http://host.docker.internal:{STUB_LLM_REVIEW_PORT}/v1",
        REVIEW_WORKER_GITHUB_API_BASE_URL=f"http://host.docker.internal:{STUB_GITHUB_PORT}",
        REVIEW_WORKER_POLL_INTERVAL_SECONDS="0.2",
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
# Webhook signing / posting helpers
# ---------------------------------------------------------------------------


def _sign(body: bytes, secret: str = _WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _webhook_payload(*, action: str, pr_number: int, head_sha: str) -> bytes:
    return json.dumps(
        {
            "installation": {"id": _INSTALLATION_ID},
            "repository": {"full_name": _OWNER_REPO},
            "action": action,
            "pull_request": {"number": pr_number, "head": {"sha": head_sha}},
        }
    ).encode()


def _post_webhook(
    env: dict[str, str], *, body: bytes, signature: str | None, event_type: str = "pull_request"
) -> int:
    port = env["WEBHOOK_API_PORT"]
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event_type,
        "X-GitHub-Delivery": str(uuid.uuid4()),
    }
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    request = urllib.request.Request(
        f"http://localhost:{port}{_WEBHOOK_URL_PATH}", data=body, method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _wait_until_calls_received(calls: _CallLog, *, timeout: float = _WAIT_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if calls.check_run_bodies:
            return
        time.sleep(0.5)
    raise AssertionError(f"no check-run posted within {timeout}s (calls so far: {calls.calls!r})")


_next_pr_number = iter(range(100, 200))


def _fresh_pr() -> tuple[int, str]:
    """A distinct PR number/head SHA for every case — see this module's own
    docstring: never reused, so idempotency/dedupe can't interfere across
    test functions sharing one Compose stack."""
    number = next(_next_pr_number)
    return number, f"{number:040x}"


# --- Happy path: the whole chain, per the roadmap's exit criteria ----------


def test_happy_path_full_chain_posts_review_and_check_run(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
    stub_llm_review_server: HTTPServer,
) -> None:
    stub_llm_review_server.mode = "success"  # type: ignore[attr-defined]
    _reset_github_scenario(stub_github_server, _single_file_scenario())
    pr_number, head_sha = _fresh_pr()

    body = _webhook_payload(action="opened", pr_number=pr_number, head_sha=head_sha)
    status = _post_webhook(running_stack, body=body, signature=_sign(body))
    assert status == 200

    calls: _CallLog = stub_github_server.calls  # type: ignore[attr-defined]
    _wait_until_calls_received(calls)

    joined_calls = "\n".join(calls.calls)
    assert "access_tokens" in joined_calls
    assert f"/repos/{_OWNER_REPO}/pulls/{pr_number}" in joined_calls
    assert f"/repos/{_OWNER_REPO}/pulls/{pr_number}/files" in joined_calls
    assert f"/repos/{_OWNER_REPO}/git/trees/" in joined_calls
    assert f"/repos/{_OWNER_REPO}/git/blobs/" in joined_calls
    assert len(calls.review_bodies) == 1
    assert calls.check_run_bodies[-1]["conclusion"] == "neutral"


# --- LLM timeout: failure-conclusion check-run, zero comments --------------


def test_llm_timeout_posts_failure_check_run_and_zero_comments(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
    stub_llm_review_server: HTTPServer,
) -> None:
    stub_llm_review_server.mode = "llm_failure"  # type: ignore[attr-defined]
    _reset_github_scenario(stub_github_server, _single_file_scenario())
    pr_number, head_sha = _fresh_pr()

    body = _webhook_payload(action="opened", pr_number=pr_number, head_sha=head_sha)
    status = _post_webhook(running_stack, body=body, signature=_sign(body))
    assert status == 200

    calls: _CallLog = stub_github_server.calls  # type: ignore[attr-defined]
    _wait_until_calls_received(calls)

    assert calls.review_bodies == []
    assert calls.check_run_bodies[-1]["conclusion"] == "failure"

    stub_llm_review_server.mode = "success"  # type: ignore[attr-defined]


# --- No findings from either analyzer or the LLM ----------------------------


def test_no_findings_posts_no_findings_neutral_check_run(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
    stub_llm_review_server: HTTPServer,
) -> None:
    stub_llm_review_server.mode = "success"  # type: ignore[attr-defined]
    _reset_github_scenario(
        stub_github_server,
        _single_file_scenario(path="README.md", content=b"# hello\n\nnothing interesting here.\n"),
    )
    pr_number, head_sha = _fresh_pr()

    body = _webhook_payload(action="opened", pr_number=pr_number, head_sha=head_sha)
    status = _post_webhook(running_stack, body=body, signature=_sign(body))
    assert status == 200

    calls: _CallLog = stub_github_server.calls  # type: ignore[attr-defined]
    _wait_until_calls_received(calls)

    assert calls.review_bodies == []
    assert calls.check_run_bodies[-1]["conclusion"] == "neutral"
    assert "no findings" in str(calls.check_run_bodies[-1]["output"]).casefold()


# --- Files-endpoint error: the E2E-level proof of Phase 7's US8-scope fix --


def test_files_endpoint_error_posts_failure_check_run_not_silent_failure(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
    stub_llm_review_server: HTTPServer,
) -> None:
    """A fatal failure *before* llm-review (here: pre-filter/diff-fetch,
    the earliest macro step) still produces a GitHub-visible signal — not
    merely a `FAILED` row in Postgres with nothing on the PR — proving
    `review.pipeline.StepRegistry`'s terminal-step mechanism holds at the
    full-stack level, not only via the directly-enqueued dispatch-loop case
    in `apps/worker/test_dispatch.py`.
    """
    stub_llm_review_server.mode = "success"  # type: ignore[attr-defined]
    scenario = _single_file_scenario()
    scenario.files_endpoint_status = 500
    _reset_github_scenario(stub_github_server, scenario)
    pr_number, head_sha = _fresh_pr()

    body = _webhook_payload(action="opened", pr_number=pr_number, head_sha=head_sha)
    status = _post_webhook(running_stack, body=body, signature=_sign(body))
    assert status == 200

    calls: _CallLog = stub_github_server.calls  # type: ignore[attr-defined]
    _wait_until_calls_received(calls)

    assert calls.review_bodies == []
    assert calls.check_run_bodies[-1]["conclusion"] == "failure"


# --- Malformed signature: regression, rejected before enqueue --------------


def test_malformed_signature_rejected_401_never_reaches_enqueue(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
    stub_llm_review_server: HTTPServer,
) -> None:
    stub_llm_review_server.mode = "success"  # type: ignore[attr-defined]
    _reset_github_scenario(stub_github_server, _single_file_scenario())
    pr_number, head_sha = _fresh_pr()
    body = _webhook_payload(action="opened", pr_number=pr_number, head_sha=head_sha)

    status = _post_webhook(running_stack, body=body, signature="sha256=" + "0" * 64)

    assert status == 401
    # Give the Worker a moment it doesn't need — the assertion is that
    # nothing *ever* reaches the stub server for this delivery, not merely
    # "not yet".
    time.sleep(2)
    calls: _CallLog = stub_github_server.calls  # type: ignore[attr-defined]
    assert calls.calls == []
