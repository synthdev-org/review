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
equivalent stub anyway). Since the verification pipeline delivery plan
extended a single review job from one LLM Gateway call to three
(`intent_summary`, `review_generation`, `verification`, in that fixed
order — the last only when `review_generation` actually produced a
`source: "llm"` finding), the stub also tracks *which* call in that
sequence it is currently answering (`_StubLLMHandler`'s own docstring
covers the mechanism) and responds with the shape that call's own step
actually expects, rather than one fixed response regardless of caller.

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
from typing import Any

import pytest

from review.fix_generation import GENERATION_MARKER, RUBRIC_MARKER
from review.walkthrough import MARKER_LINE as WALKTHROUGH_MARKER_LINE
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


_APP_ID = 123456  # REVIEW_WORKER_GITHUB_APP_ID in tests/e2e/conftest.py
_COMMENT_LIMIT_TOTAL = 501  # one more than the adapter's 5 pages of 100


@dataclass
class _IssueComments:
    """PR issue-comment state for the walkthrough routes (plan-phase-12 Phase 5).

    `mode`: `normal`; `write_error` (POST and PATCH answer 500); `limit` (the list
    serves 501 foreign comments over six pages, so the adapter hits its page cap).
    `writes` records every non-GET call, as `METHOD path`."""

    items: list[dict[str, object]] = field(default_factory=list)
    mode: str = "normal"
    next_id: int = 1000
    writes: list[str] = field(default_factory=list)

    def platform_bodies(self) -> list[str]:
        return [str(c["body"]) for c in self.items]


def serve_issue_comments(
    handler: BaseHTTPRequestHandler, method: str, path: str, query: str, owner_repo: str
) -> bool:
    """Answers the issue-comment list/create/edit/delete routes on `handler`
    (which needs `_send_json`/`_read_json_body` and `server.issue_comments`).
    Returns False when `path` is not one of them. Shared by every E2E stub GitHub."""
    store = handler.server.issue_comments  # type: ignore[attr-defined]
    assert isinstance(store, _IssueComments)
    send = handler._send_json  # type: ignore[attr-defined]
    owner = re.escape(owner_repo)
    listing = re.fullmatch(rf"/repos/{owner}/issues/\d+/comments", path)
    single = re.fullmatch(rf"/repos/{owner}/issues/comments/(\d+)", path)
    if not (listing or single):
        return False
    if method != "GET":
        store.writes.append(f"{method} {path}")

    if listing and method == "GET":
        if store.mode == "limit":
            page = int(next(iter(re.findall(r"(?:^|&)page=(\d+)", query)), "1"))
            lo, hi = (page - 1) * 100, min(page * 100, _COMMENT_LIMIT_TOTAL)
            foreign = [{"id": i + 1, "body": f"human comment {i}"} for i in range(lo, hi)]
            data = json.dumps(foreign).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            if hi < _COMMENT_LIMIT_TOTAL:
                host = handler.headers.get("Host", "")
                nxt = f"http://{host}{path}?per_page=100&page={page + 1}"
                handler.send_header("Link", f'<{nxt}>; rel="next"')
            handler.end_headers()
            handler.wfile.write(data)
            return True
        send(200, store.items)
        return True
    if store.mode == "write_error" and method in ("POST", "PATCH"):
        send(500, {"message": "simulated comment write failure (stub)"})
        return True
    body = str(handler._read_json_body().get("body", ""))  # type: ignore[attr-defined]
    if listing and method == "POST":
        store.next_id += 1
        comment: dict[str, object] = {
            "id": store.next_id,
            "body": body,
            "performed_via_github_app": {"id": _APP_ID},
        }
        store.items.append(comment)
        send(201, comment)
        return True
    assert single is not None
    cid = int(single.group(1))
    target = next((c for c in store.items if c["id"] == cid), None)
    if target is None:
        send(404, {"message": "not found (stub)"})
    elif method == "PATCH":
        target["body"] = body
        send(200, target)
    elif method == "DELETE":
        store.items.remove(target)
        handler.send_response(204)
        handler.end_headers()
    else:
        send(405, {"message": "method not allowed (stub)"})
    return True


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

        if serve_issue_comments(self, "POST", path, "", _OWNER_REPO):
            return

        self._send_json(404, {"message": "not found (stub)"})

    def do_PATCH(self) -> None:
        self._calls().calls.append(f"PATCH {self.path}")
        if not serve_issue_comments(self, "PATCH", self.path, "", _OWNER_REPO):
            self._send_json(404, {"message": "not found (stub)"})

    def do_DELETE(self) -> None:
        self._calls().calls.append(f"DELETE {self.path}")
        if not serve_issue_comments(self, "DELETE", self.path, "", _OWNER_REPO):
            self._send_json(404, {"message": "not found (stub)"})

    def do_GET(self) -> None:
        path_only = self.path.split("?", 1)[0]
        self._calls().calls.append(f"GET {self.path}")
        scenario = self._scenario()

        query = self.path.partition("?")[2]
        if serve_issue_comments(self, "GET", path_only, query, _OWNER_REPO):
            return

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
    server.issue_comments = _IssueComments()  # type: ignore[attr-defined]
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
    startup reachability check, whatever else LiteLLM sends).

    A single review job now makes up to three calls to this endpoint, in a
    fixed order — `intent_summary`, then `review_generation`, then (only
    when `review_generation` actually produced at least one
    `source: "llm"` finding) `verification` — and each expects a
    differently-shaped response (plain text; `{"findings": [...]}`;
    `{"verdicts": [...]}`, respectively). This handler tracks a per-test
    call counter on `self.server.call_count` (reset by `_reset_llm_scenario`
    at the start of every test function, not only once at server startup —
    see that helper's own docstring for why) so it can answer each call in
    a job's own sequence with the shape *that* call's own step actually
    expects, rather than the single fixed `{"findings": []}` completion
    every call used to receive regardless of which step made it — a stub
    that couldn't have caught a wiring regression silently skipping
    `intent_summary`/`verification` entirely.

    `self.server.mode` still gates the pre-existing `"success"`/
    `"llm_failure"` behavior: a `"llm_failure"` response (`500`) is
    returned unconditionally, regardless of call position — this is what
    the LLM-timeout/failure test case still needs (the very first LLM call
    in the chain, whichever step makes it, must fail). Only under
    `"success"` does call position matter: `self.server.
    review_generation_scenario` (`"zero_findings"` — the default, matching
    every pre-existing test's expectations — or `"one_finding"`) picks
    `review_generation`'s own response; `self.server.verification_scenario`
    (`"verified"` — the default — or `"rejected"`) picks `verification`'s
    own response, for whichever test cases actually reach that third call.
    """

    _INTENT_SUMMARY_CONTENT = "This PR makes a small, self-contained change."
    _ZERO_FINDINGS_CONTENT = json.dumps({"findings": []})
    _ONE_FINDING_CONTENT = json.dumps(
        {
            "findings": [
                {
                    "file": "README.md",
                    "line": 3,
                    "severity": "medium",
                    "message": "This section could clarify the expected behavior further.",
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
    _VERDICT_REJECTED_CONTENT = json.dumps(
        {
            "verdicts": [
                {
                    "index": 0,
                    "valid": True,
                    "actionable": True,
                    "specific": False,
                    "valuable": True,
                }
            ]
        }
    )

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _completion_body(self, content: str) -> bytes:
        return json.dumps(
            {
                "id": "chatcmpl-e2e-review-stub",
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

    def _content_for_call(self, call_number: int) -> str:
        """`call_number` is this server's own 1-based position within the
        current test's own sequence (per `_reset_llm_scenario`'s reset) —
        1 = `intent_summary`, 2 = `review_generation`, 3 (or beyond, though
        no real job ever makes a fourth call) = `verification`."""
        if call_number <= 1:
            return self._INTENT_SUMMARY_CONTENT
        if call_number == 2:
            custom = getattr(self.server, "review_generation_content", None)
            if custom is not None:
                return str(custom)
            scenario = getattr(self.server, "review_generation_scenario", "zero_findings")
            return (
                self._ONE_FINDING_CONTENT
                if scenario == "one_finding"
                else self._ZERO_FINDINGS_CONTENT
            )
        scenario = getattr(self.server, "verification_scenario", "verified")
        if scenario == "rejected":
            return self._VERDICT_REJECTED_CONTENT
        return self._VERDICT_VERIFIED_CONTENT

    def _fix_content(self, request: dict[str, Any]) -> str | None:
        """Auto-fix calls (plan-phase-11 Assumption 15) are recognized by the
        fixed marker in their system message, not by call position: they come
        after `verification`, in variable number and concurrently. Answered from
        `self.server.fix_scenario` (`{path: {"generation": ..., "rubric": ...}}`);
        a path with no entry gets an unparseable generation reply, which the
        step rejects. Returns None for any other call."""
        system = next(
            (m["content"] for m in request.get("messages", []) if m.get("role") == "system"), ""
        )
        for marker, key in ((GENERATION_MARKER, "generation"), (RUBRIC_MARKER, "rubric")):
            if marker not in str(system):
                continue
            user = "".join(
                m["content"] for m in request.get("messages", []) if m.get("role") == "user"
            )
            match = re.search(r"^File (\S+), numbered lines", user, re.MULTILINE)
            path = match.group(1) if match else "?"
            self.server.fix_calls.append(f"{key}:{path}")  # type: ignore[attr-defined]
            scenario = getattr(self.server, "fix_scenario", {})
            return str(scenario.get(path, {}).get(key, "not json"))
        return None

    def _walkthrough_content(self, request: dict[str, Any]) -> str | None:
        """The walkthrough call (plan-phase-12 Phase 5) is recognized by its fixed
        marker line, not call position, and never advances `call_count`. Scenario
        `self.server.walkthrough_scenario`: `ok` (one group over every path sent),
        `garbage` (not JSON) or `delay` (sleeps past the Worker's LLM timeout, then
        answers `ok`)."""
        messages = request.get("messages", [])
        system = " ".join(str(m["content"]) for m in messages if m.get("role") == "system")
        if WALKTHROUGH_MARKER_LINE not in system:
            return None
        user = "".join(str(m["content"]) for m in messages if m.get("role") == "user")
        self.server.walkthrough_calls += 1  # type: ignore[attr-defined]
        scenario = getattr(self.server, "walkthrough_scenario", "ok")
        if scenario == "garbage":
            return "not json"
        if scenario == "delay":
            time.sleep(float(getattr(self.server, "walkthrough_delay_seconds", 15.0)))
        paths = re.findall(r"^--- (.+) \(\w+, \+\d+ -\d+\)$", user, re.MULTILINE)
        files = [{"path": p, "note": "Changes " + p} for p in paths]
        return json.dumps(
            {"groups": [{"name": "Core changes", "description": "The change.", "files": files}]}
        )

    def do_POST(self) -> None:
        mode = getattr(self.server, "mode", "success")
        if mode == "llm_failure":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "simulated failure"}')
            return

        length = int(self.headers.get("Content-Length", "0"))
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            request = {}
        fix_content = self._fix_content(request) if isinstance(request, dict) else None
        if fix_content is None and isinstance(request, dict):
            fix_content = self._walkthrough_content(request)
        if fix_content is not None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(self._completion_body(fix_content))
            return

        call_number = getattr(self.server, "call_count", 0) + 1
        self.server.call_count = call_number  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self._completion_body(self._content_for_call(call_number)))


@pytest.fixture(scope="module")
def stub_llm_review_server() -> Iterator[HTTPServer]:
    server = HTTPServer(("0.0.0.0", STUB_LLM_REVIEW_PORT), _StubLLMHandler)
    server.mode = "success"  # type: ignore[attr-defined]
    server.call_count = 0  # type: ignore[attr-defined]
    server.review_generation_scenario = "zero_findings"  # type: ignore[attr-defined]
    server.verification_scenario = "verified"  # type: ignore[attr-defined]
    server.review_generation_content = None  # type: ignore[attr-defined]
    server.fix_scenario = {}  # type: ignore[attr-defined]
    server.walkthrough_scenario = "ok"  # type: ignore[attr-defined]
    server.walkthrough_calls = 0  # type: ignore[attr-defined]
    server.fix_calls = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _reset_llm_scenario(
    server: HTTPServer,
    *,
    mode: str = "success",
    review_generation: str = "zero_findings",
    verification: str = "verified",
    review_generation_content: str | None = None,
    fix_scenario: dict[str, dict[str, str]] | None = None,
    walkthrough: str = "ok",
) -> None:
    """Resets the stub LLM Gateway server's per-test scenario state —
    mirrors `_reset_github_scenario`'s own shape. Called at the start of
    every test function, not only once at server startup: the Worker's own
    one-time startup LLM-reachability check (`llmgateway/startup_check.py`)
    fires once when the Compose stack first comes up (module-scoped
    `running_stack` fixture), before any individual test's own reset —
    resetting `call_count` to `0` here keeps each test's own call-position
    numbering (call 1 = `intent_summary`, call 2 = `review_generation`,
    call 3 = `verification`) independent of that earlier startup call and
    of every other test sharing this one module-scoped server.
    """
    server.mode = mode  # type: ignore[attr-defined]
    server.call_count = 0  # type: ignore[attr-defined]
    server.review_generation_scenario = review_generation  # type: ignore[attr-defined]
    server.verification_scenario = verification  # type: ignore[attr-defined]
    server.review_generation_content = review_generation_content  # type: ignore[attr-defined]
    server.fix_scenario = fix_scenario or {}  # type: ignore[attr-defined]
    server.fix_calls = []  # type: ignore[attr-defined]
    server.walkthrough_scenario = walkthrough  # type: ignore[attr-defined]
    server.walkthrough_calls = 0  # type: ignore[attr-defined]


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
    _reset_llm_scenario(stub_llm_review_server)
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
    _reset_llm_scenario(stub_llm_review_server, mode="llm_failure")
    _reset_github_scenario(stub_github_server, _single_file_scenario())
    pr_number, head_sha = _fresh_pr()

    body = _webhook_payload(action="opened", pr_number=pr_number, head_sha=head_sha)
    status = _post_webhook(running_stack, body=body, signature=_sign(body))
    assert status == 200

    calls: _CallLog = stub_github_server.calls  # type: ignore[attr-defined]
    _wait_until_calls_received(calls)

    assert calls.review_bodies == []
    assert calls.check_run_bodies[-1]["conclusion"] == "failure"


# --- No findings from either analyzer or the LLM ----------------------------


def test_no_findings_posts_no_findings_neutral_check_run(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
    stub_llm_review_server: HTTPServer,
) -> None:
    _reset_llm_scenario(stub_llm_review_server)
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
    _reset_llm_scenario(stub_llm_review_server)
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
    _reset_llm_scenario(stub_llm_review_server)
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


# --- Verified LLM finding: proves the six-step chain actually ran ----------


def test_verified_llm_finding_reaches_github_after_three_llm_calls(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
    stub_llm_review_server: HTTPServer,
) -> None:
    """The traceability-table gap this test (and the rejected-finding case
    below) closes: the pre-existing happy-path test only proved the
    pipeline *completes* with a zero-findings response every LLM Gateway
    call answered identically — it would have kept passing unchanged even
    if `intent_summary`/`verification` were silently disabled or wired to
    the wrong upstream dependency (e.g. a broken
    `review.config.resolve_review_pipeline_depends_on`). Asserting all
    three LLM Gateway calls actually happened, plus the one surviving,
    verified finding actually reaching the posted PR review, proves the
    real six-step chain (`prefilter -> static_analysis -> context_builder
    -> intent_summary -> review_generation -> verification -> output`) ran
    end-to-end, not merely that *some* chain producing zero findings did.
    """
    _reset_llm_scenario(
        stub_llm_review_server, review_generation="one_finding", verification="verified"
    )
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

    # All three LLM Gateway calls really happened: intent_summary,
    # review_generation, and verification each ran, in that order — not
    # silently skipped by a wiring regression.
    assert getattr(stub_llm_review_server, "call_count", 0) == 3
    assert calls.check_run_bodies[-1]["conclusion"] == "neutral"
    assert len(calls.review_bodies) == 1
    comments = calls.review_bodies[-1]["comments"]
    assert isinstance(comments, list)
    assert len(comments) == 1


# --- Rejected LLM finding: verification ran but the finding never posts ----


def test_rejected_llm_finding_never_reaches_github(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
    stub_llm_review_server: HTTPServer,
) -> None:
    """The mirror image of the verified-finding case above: verification
    still runs its one, structurally separate rubric-judgment call (proven
    by the 3-call assertion below — this is not merely "verification was
    skipped and nothing was posted anyway"), but a finding failing at least
    one of the four rubric criteria (here: `specific=False`) is dropped
    before `findings-merge-and-output` ever sees it (US-V4). With no other
    findings in this scenario (`README.md`, a static-analysis-clean file),
    the merged findings list is empty — `review/output.py`'s own
    `FindingsOutputStep._run_happy_path` only calls `post_review(...)` when
    `findings` is non-empty (`if findings:`), so `post_review` is never
    called at all, not called with a zero-comment body.
    """
    _reset_llm_scenario(
        stub_llm_review_server, review_generation="one_finding", verification="rejected"
    )
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

    # Verification's own LLM call really happened (rubric judgment ran, it
    # just rejected the one candidate finding) — not skipped.
    assert getattr(stub_llm_review_server, "call_count", 0) == 3
    assert calls.review_bodies == []
    assert calls.check_run_bodies[-1]["conclusion"] == "neutral"
    assert "no findings" in str(calls.check_run_bodies[-1]["output"]).casefold()
