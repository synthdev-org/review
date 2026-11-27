"""E2E: the Memory feature's happy path end to end. The manifest's new
event subscriptions are proven at unit tier and aren't re-tested here,
since this file drives webhooks directly over HTTP rather than through a
real GitHub App delivery.

Follows ``tests/e2e/test_review_pipeline.py``'s established shape exactly:
one module-scoped Docker Compose stack (Webhook API + Admin API + Worker +
Postgres — Admin API since US-A7 moved the Memory admin routes the approval
step below drives onto it),
a self-contained stub GitHub server and a self-contained stub LLM Gateway
server (self-contained per that file's own docstring reasoning — this
file's scenario/call-log needs differ enough from ``tests/e2e/conftest.py``'s
shared ``stub_llm_server`` that reusing it would mean either mutating
shared package fixture state other tests don't expect, or a second,
equivalent stub anyway), synthetic signed webhook payloads in, real HTTP
assertions out.

**Scope: happy path only** (US-M1-M12). The kill switch (US-M12) already has
its own dedicated Integration-tier test
(``review/test_verification_step.py``, which owns US-M12) — bringing up a
*second* Compose stack here just to re-prove
``memory_suppression_enabled=false`` restores prior behavior would be an
expensive, redundant repeat of coverage that tier already owns, not a gap
this file needs to close (testing.md's own E2E tier is "few," by design).

One script, one test function — not several independent ones — since the
story is inherently sequential and stateful: a review is posted (US-M11),
a reply and a thread-resolution are captured on that same posted comment
(US-M1, US-M2), a reaction is captured for it via a synchronous PR-closed poll
(US-M3), a human approves it as a ``dismissed_finding`` through the real
admin API which computes a real local-ONNX embedding (US-M4/US-M8), and a
second PR producing the *identical* finding (same file/rule_id/message, so
an identical fingerprint and, from the same deterministic local model, an
effectively identical embedding) is suppressed before ``VerificationStep``'s
rubric call ever runs (US-M6/US-M7) — each step's own precondition is the real
system state the previous step actually produced, not a hand-seeded
fixture.
"""

from __future__ import annotations

import hashlib
import hmac
import http.cookiejar
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
STUB_GITHUB_PORT = 19083
STUB_LLM_REVIEW_PORT = 19236
_OWNER_REPO = "acme/widgets"
_INSTALLATION_ID = 123456
_WEBHOOK_URL_PATH = "/webhooks/github"
_WEBHOOK_SECRET = "e2e-test-webhook-secret"
# US-A7 retired the static `admin_api_key` Bearer token this file used to
# send in favor of a real per-user session cookie plus a CSRF token every
# mutating request must echo back — these two constants are this black-box HTTP test's own copy
# of apps/admin_api/dependencies.py's `CSRF_COOKIE_NAME`/`CSRF_HEADER_NAME`
# wire contract (not an in-process import of that module, to keep this file
# genuinely HTTP-only, per its own module docstring).
_CSRF_COOKIE_NAME = "review_admin_csrf"
_CSRF_HEADER_NAME = "X-CSRF-Token"
_WAIT_TIMEOUT_SECONDS = 60.0

_FILE_PATH = "README.md"
_FILE_CONTENT = b"# hello\n\nnothing interesting here.\n"


# ---------------------------------------------------------------------------
# Stub GitHub server — the whole call chain the review and PR-closed paths
# need, plus the ordinary diff-fetch/posting chain every review job makes.
# ---------------------------------------------------------------------------


@dataclass
class _CallLog:
    calls: list[str] = field(default_factory=list)
    review_bodies: list[dict[str, object]] = field(default_factory=list)
    check_run_bodies: list[dict[str, object]] = field(default_factory=list)
    posted_comments: list[dict[str, object]] = field(default_factory=list)
    """Every comment GitHub "assigned an id to" across every posted review
    this run — flattened, newest last, so the test can read the most
    recently posted comment's own real id straight off this list instead of
    re-deriving it from a raw review-response body."""


def _blob_sha() -> str:
    return "f" * 40


def _single_file_scenario() -> dict[str, object]:
    return {
        "files": [{"filename": _FILE_PATH, "status": "added", "patch": "@@ -0,0 +1 @@\n+planted"}],
        "tree": [
            {"path": _FILE_PATH, "mode": "100644", "sha": _blob_sha(), "size": len(_FILE_CONTENT)}
        ],
        "blobs": {_blob_sha(): _FILE_CONTENT},
    }


class _StubGitHubHandler(BaseHTTPRequestHandler):
    """Answers installation-token exchange, the diff-fetch chain (PR object,
    Files endpoint, Git Trees, Git Blobs), review/check-run posting, the
    follow-up comments-GET, and the
    per-comment reactions-GET — everything both the Worker (review jobs) and
    the Webhook API (the synchronous ``pull_request closed`` reaction poll) call against GitHub.

    Mutable per-run state (scenario, call log, assigned review/comment ids,
    configured reactions) lives on ``self.server`` — a fresh handler
    instance is constructed per request, so it can't hold state itself.
    """

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
                201, {"token": f"ghs_mem_{uuid.uuid4().hex}", "expires_at": "2999-01-01T00:00:00Z"}
            )
            return

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+/reviews", path):
            body = self._read_json_body()
            self._calls().review_bodies.append(body)
            review_id = self.server.next_review_id  # type: ignore[attr-defined]
            self.server.next_review_id = review_id + 1  # type: ignore[attr-defined]
            raw_comments = body.get("comments", [])
            assert isinstance(raw_comments, list)
            comments_with_ids = []
            for raw_comment in raw_comments:
                assert isinstance(raw_comment, dict)
                comment_id = self.server.next_comment_id  # type: ignore[attr-defined]
                self.server.next_comment_id = comment_id + 1  # type: ignore[attr-defined]
                with_id = {**raw_comment, "id": comment_id}
                comments_with_ids.append(with_id)
                self._calls().posted_comments.append(with_id)
            self.server.reviews[review_id] = comments_with_ids  # type: ignore[attr-defined]
            self._send_json(200, {"id": review_id})
            return

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/check-runs", path):
            self._calls().check_run_bodies.append(self._read_json_body())
            self._send_json(201, {"id": 1})
            return

        self._send_json(404, {"message": "not found (stub)"})

    def do_GET(self) -> None:
        path_only = self.path.split("?", 1)[0]
        self._calls().calls.append(f"GET {self.path}")
        scenario = getattr(self.server, "scenario", None)
        assert isinstance(scenario, dict)

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+", path_only):
            self._send_json(200, {"changed_files": len(scenario["files"])})
            return

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+/files", path_only):
            self._send_json(200, scenario["files"])
            return

        if re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/git/trees/\w+", path_only):
            self._send_json(200, {"tree": scenario["tree"], "truncated": False})
            return

        match = re.fullmatch(rf"/repos/{re.escape(_OWNER_REPO)}/git/blobs/(\w+)", path_only)
        if match:
            from base64 import b64encode

            blobs = scenario["blobs"]
            assert isinstance(blobs, dict)
            content = blobs.get(match.group(1), b"")
            self._send_json(
                200, {"content": b64encode(content).decode("ascii"), "encoding": "base64"}
            )
            return

        match = re.fullmatch(
            rf"/repos/{re.escape(_OWNER_REPO)}/pulls/\d+/reviews/(\d+)/comments", path_only
        )
        if match:
            reviews: dict[int, list[dict[str, object]]] = self.server.reviews  # type: ignore[attr-defined]
            self._send_json(200, reviews.get(int(match.group(1)), []))
            return

        match = re.fullmatch(
            rf"/repos/{re.escape(_OWNER_REPO)}/pulls/comments/(\d+)/reactions", path_only
        )
        if match:
            reactions: dict[int, list[dict[str, object]]] = self.server.reactions  # type: ignore[attr-defined]
            self._send_json(200, reactions.get(int(match.group(1)), []))
            return

        self._send_json(404, {"message": "not found (stub)"})


@pytest.fixture(scope="module")
def stub_github_server() -> Iterator[HTTPServer]:
    server = HTTPServer(("0.0.0.0", STUB_GITHUB_PORT), _StubGitHubHandler)
    server.scenario = _single_file_scenario()  # type: ignore[attr-defined]
    server.calls = _CallLog()  # type: ignore[attr-defined]
    server.reviews = {}  # type: ignore[attr-defined]
    server.reactions = {}  # type: ignore[attr-defined]
    server.next_review_id = 1  # type: ignore[attr-defined]
    server.next_comment_id = 1  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _reset_github_calls(server: HTTPServer) -> None:
    """Clears only the call log between review-triggering webhook posts, so
    each PR's own review-posting behavior can be asserted in isolation —
    deliberately leaves ``reviews``/``next_comment_id`` untouched, since
    earlier PRs' already-captured comment ids remain real, valid state the
    rest of the flow (replies, thread resolution, reactions, admin approval)
    still depends on. Clears the existing ``_CallLog``'s lists in place
    (never reassigns ``server.calls`` to a fresh instance) — the test
    function holds its own ``calls`` reference from the top of the test, and
    a reassignment here would silently desync that reference from what the
    server actually appends to next."""
    calls: _CallLog = server.calls  # type: ignore[attr-defined]
    calls.calls.clear()
    calls.review_bodies.clear()
    calls.check_run_bodies.clear()
    calls.posted_comments.clear()


# ---------------------------------------------------------------------------
# Stub LLM Gateway server — self-contained to this file (see module
# docstring). Deterministic, identical output for both PRs' review jobs by
# design: the whole suppression assertion (US-M6/US-M7) depends on the second
# PR's candidate finding sharing an exact fingerprint with the first.
# ---------------------------------------------------------------------------


class _StubLLMHandler(BaseHTTPRequestHandler):
    """A fixed three-call sequence per review job — ``intent_summary``,
    ``review_generation`` (always the one, fixed finding below), then
    ``verification`` (always ``verified``) — tracked via a per-server
    ``call_count`` the test resets before each PR's own webhook post."""

    _INTENT_SUMMARY_CONTENT = "This PR makes a small, self-contained change."
    _ONE_FINDING_CONTENT = json.dumps(
        {
            "findings": [
                {
                    "file": _FILE_PATH,
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

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _completion_body(self, content: str) -> bytes:
        return json.dumps(
            {
                "id": "chatcmpl-e2e-memory-stub",
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
        if call_number <= 1:
            return self._INTENT_SUMMARY_CONTENT
        if call_number == 2:
            return self._ONE_FINDING_CONTENT
        return self._VERDICT_VERIFIED_CONTENT

    def do_POST(self) -> None:
        call_number = getattr(self.server, "call_count", 0) + 1
        self.server.call_count = call_number  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self._completion_body(self._content_for_call(call_number)))


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


def _reset_llm_call_count(server: HTTPServer) -> None:
    """Reset before each PR's own webhook post, mirroring
    ``tests/e2e/test_review_pipeline.py``'s own ``_reset_llm_scenario`` —
    the Worker's one-time startup LLM-reachability probe (fired once when
    ``running_stack`` first comes up) must not count toward either PR's own
    3-call-vs-2-call assertion below."""
    server.call_count = 0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Compose stack
# ---------------------------------------------------------------------------


def _e2e_env() -> dict[str, str]:
    return base_compose_env(
        REVIEW_WORKER_LLM_ENDPOINT=f"http://host.docker.internal:{STUB_LLM_REVIEW_PORT}/v1",
        REVIEW_WORKER_GITHUB_API_BASE_URL=f"http://host.docker.internal:{STUB_GITHUB_PORT}",
        REVIEW_WEBHOOK_API_GITHUB_API_BASE_URL=f"http://host.docker.internal:{STUB_GITHUB_PORT}",
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
        # The approval step now drives apps/admin_api (US-A7) instead
        # of webhook_api's retired static-token route — its own healthcheck
        # (docker-compose.yml) only reports healthy once the local ONNX
        # embedding model has finished loading, so waiting here (rather
        # than discovering readiness via the first failed login later in
        # the test) keeps that startup cost off the test's own assertions.
        wait_for_healthy("admin_api", env)
        wait_for_log_line("worker", env, "worker started")
        yield env
    finally:
        logs = compose("logs", "--no-color", env=env).stdout
        print(logs)
        compose_down(env)


# ---------------------------------------------------------------------------
# Webhook signing / posting / admin-API session helpers
# ---------------------------------------------------------------------------


def _sign(body: bytes, secret: str = _WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _post_webhook(env: dict[str, str], *, event_type: str, payload: dict[str, object]) -> int:
    body = json.dumps(payload).encode()
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


def _pull_request_payload(
    *, action: str, pr_number: int, head_sha: str, extra: dict[str, object] | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "installation": {"id": _INSTALLATION_ID},
        "repository": {"full_name": _OWNER_REPO},
        "action": action,
        "pull_request": {"number": pr_number, "head": {"sha": head_sha}},
    }
    if extra:
        payload.update(extra)
    return payload


class _AdminSession:
    """A real, logged-in ``apps.admin_api`` HTTP session.

    US-A7 moved the Memory admin API from ``apps/webhook_api`` (a static
    ``admin_api_key`` Bearer token) to ``apps/admin_api`` (real per-user
    sessions, ``require_session``/``require_admin_role``) — this is that
    migration's E2E-tier, real-HTTP equivalent of
    ``review/test_verification_step.py``'s own ``_admin_client``/
    ``_login_as_contract_admin`` helpers, which drive the same app
    in-process via ``TestClient`` instead of over the wire against the
    Compose stack's own ``admin_api`` container.

    Logs in once (the bootstrap admin ``conftest.base_compose_env`` already
    provisions via ``REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_USERNAME``/
    ``PASSWORD``), keeps the session cookie in a cookie jar, and echoes the
    CSRF cookie's value back on every subsequent request via
    ``_CSRF_HEADER_NAME`` — cheap to do unconditionally even for the one
    GET the test below makes, since ``apps/admin_api/dependencies.py``'s
    own CSRF check only actually enforces it on mutating methods.
    """

    def __init__(self, env: dict[str, str]) -> None:
        self._port = env["ADMIN_API_PORT"]
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._csrf_token = self._login(
            username=env["REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_USERNAME"],
            password=env["REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_PASSWORD"],
        )

    def _login(self, *, username: str, password: str) -> str:
        status, _ = self._raw_request(
            method="POST",
            path="/api/v1/auth/login",
            body={"username": username, "password": password},
        )
        assert status == 200, f"admin bootstrap login failed with status {status}"
        for cookie in self._jar:
            if cookie.name == _CSRF_COOKIE_NAME:
                assert cookie.value is not None
                return cookie.value
        raise AssertionError("admin login response did not set the CSRF cookie")

    def _raw_request(
        self, *, method: str, path: str, body: dict[str, object] | None, csrf_token: str = ""
    ) -> tuple[int, dict[str, object]]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if csrf_token:
            headers[_CSRF_HEADER_NAME] = csrf_token
        request = urllib.request.Request(
            f"http://localhost:{self._port}{path}", data=data, method=method, headers=headers
        )
        try:
            with self._opener.open(request, timeout=15) as response:
                raw = response.read()
                return int(response.status), (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return int(exc.code), (json.loads(raw) if raw else {})

    def request(
        self, *, method: str, path: str, body: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object]]:
        return self._raw_request(method=method, path=path, body=body, csrf_token=self._csrf_token)


def _wait_until_check_run_count(
    calls: _CallLog, count: int, *, timeout: float = _WAIT_TIMEOUT_SECONDS
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(calls.check_run_bodies) >= count:
            return
        time.sleep(0.5)
    raise AssertionError(
        f"expected {count} check-run(s) within {timeout}s, got {len(calls.check_run_bodies)} "
        f"(calls so far: {calls.calls!r})"
    )


_next_pr_number = iter(range(300, 400))


def _fresh_pr() -> tuple[int, str]:
    number = next(_next_pr_number)
    return number, f"{number:040x}"


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_memory_pipeline_happy_path_end_to_end(
    running_stack: dict[str, str],
    stub_github_server: HTTPServer,
    stub_llm_review_server: HTTPServer,
) -> None:
    calls: _CallLog = stub_github_server.calls  # type: ignore[attr-defined]
    pr_a_number, pr_a_head_sha = _fresh_pr()

    # --- US-M11: a review posts, provenance is captured ----------
    _reset_llm_call_count(stub_llm_review_server)
    status = _post_webhook(
        running_stack,
        event_type="pull_request",
        payload=_pull_request_payload(
            action="opened", pr_number=pr_a_number, head_sha=pr_a_head_sha
        ),
    )
    assert status == 200
    _wait_until_check_run_count(calls, 1)
    assert len(calls.review_bodies) == 1
    assert calls.check_run_bodies[-1]["conclusion"] == "neutral"
    assert len(calls.posted_comments) == 1, "exactly one finding should have been posted"
    posted_comment_id = calls.posted_comments[-1]["id"]
    assert isinstance(posted_comment_id, int)

    # --- US-M1: a reply to that posted comment is captured -------
    reply_status = _post_webhook(
        running_stack,
        event_type="pull_request_review_comment",
        payload=_pull_request_payload(
            action="created",
            pr_number=pr_a_number,
            head_sha=pr_a_head_sha,
            extra={
                "comment": {
                    "id": 900001,
                    "in_reply_to_id": posted_comment_id,
                    "body": "I don't think this applies to this line.",
                }
            },
        ),
    )
    assert reply_status == 200

    # --- US-M2: a thread-resolution on the same comment ----------
    resolution_status = _post_webhook(
        running_stack,
        event_type="pull_request_review_thread",
        payload=_pull_request_payload(
            action="resolved",
            pr_number=pr_a_number,
            head_sha=pr_a_head_sha,
            extra={"thread": {"comments": [{"id": posted_comment_id}]}},
        ),
    )
    assert resolution_status == 200

    # --- US-M3 (second branch): PR-closed triggers a synchronous --
    # --- reaction poll against the same posted comment ---------------------
    stub_github_server.reactions[posted_comment_id] = [  # type: ignore[attr-defined]
        {"id": 700001, "content": "+1"}
    ]
    close_status = _post_webhook(
        running_stack,
        event_type="pull_request",
        payload=_pull_request_payload(
            action="closed", pr_number=pr_a_number, head_sha=pr_a_head_sha
        ),
    )
    assert close_status == 200

    # --- US-M4/US-M8: the admin API lists what the steps above just ------
    # --- captured, and approving one computes a real local-ONNX embedding -
    # Real login against apps/admin_api, per US-A7's
    # retirement of the old static admin_api_key route on webhook_api.
    admin_session = _AdminSession(running_stack)
    entry_id: str | None = None
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    pending_entries: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        list_status, list_body = admin_session.request(
            method="GET", path="/api/v1/memory-entries?status=pending&limit=50"
        )
        assert list_status == 200
        raw_entries = list_body.get("entries", [])
        assert isinstance(raw_entries, list)
        pending_entries = raw_entries
        matching = [
            e for e in pending_entries if e.get("posted_finding_comment_id") == posted_comment_id
        ]
        if len(matching) >= 3:
            entry_id = str(matching[0]["id"])
            break
        time.sleep(1)
    assert entry_id is not None, (
        f"expected at least 3 pending entries (reply, thread_resolution, reaction) for comment "
        f"{posted_comment_id} within {_WAIT_TIMEOUT_SECONDS}s; got {pending_entries!r}"
    )
    raw_signal_types = {
        e["raw_signal_type"]
        for e in pending_entries
        if e.get("posted_finding_comment_id") == posted_comment_id
    }
    assert raw_signal_types == {"reply", "thread_resolution", "reaction"}

    approve_status, approved_entry = admin_session.request(
        method="POST",
        path=f"/api/v1/memory-entries/{entry_id}/approve",
        body={"entry_type": "dismissed_finding"},
    )
    assert approve_status == 200
    assert approved_entry["status"] == "approved"
    assert approved_entry["entry_type"] == "dismissed_finding"
    embedding = approved_entry["embedding"]
    assert isinstance(embedding, list)
    assert len(embedding) == 384

    # --- US-M6/US-M7: a second PR producing the identical -------
    # --- finding is suppressed before the rubric ever runs ----------------
    _reset_github_calls(stub_github_server)
    _reset_llm_call_count(stub_llm_review_server)
    pr_b_number, pr_b_head_sha = _fresh_pr()
    status_b = _post_webhook(
        running_stack,
        event_type="pull_request",
        payload=_pull_request_payload(
            action="opened", pr_number=pr_b_number, head_sha=pr_b_head_sha
        ),
    )
    assert status_b == 200
    _wait_until_check_run_count(calls, 1)

    assert calls.review_bodies == [], "the suppressed finding must never reach a posted review"
    assert calls.check_run_bodies[-1]["conclusion"] == "neutral"
    # Only intent_summary + review_generation ran — verification's own
    # rubric call was never made, per US-M6's "no LLM call spent on it": the
    # finding was removed from the candidate batch before that third call.
    assert getattr(stub_llm_review_server, "call_count", 0) == 2
