"""
E2E scenario for docs/plans/plan-deliver-product-knowledge.md Phase 9 (Unit I).

US-PK1-7 happy path plus the provider-down failure mode that matters, over the real Docker Compose
stack (Webhook API + Worker + Admin API + Postgres).

**Fixture order (plan Phase 9, Assumption 9).** The fake product-knowledge
provider is a *host process* (the ``stub_llm_server`` precedent in
``conftest.py``): it is started on a known port bound to every interface, the
credential map naming it (``allowed_hosts=["host.docker.internal"]``) is put in
the Compose environment, and only then is the stack brought up. It relies on
``product_knowledge_allow_private_networks=true`` (the default), because
``host.docker.internal`` resolves to the Docker host-gateway address.

**Scenario, one stack, three PR reviews, one stub GitHub, one stub LLM:**

1. PR #1, no provider configured: the review is posted with no
   ``**Product knowledge`` block and the provider saw **zero** requests
   (US-PK4 / zero-config).
2. The provider is configured at *repo* scope through the real Admin API
   (``PUT /api/v1/config/repo:<org>:<repo>/product_knowledge_provider``; the
   org/repo ids come from ``GET /api/v1/config`` once PR #1 resolved them).
   PR #2 on the private repo, whose title names ``HR-77``, is reviewed: the
   posted finding carries ``**Product knowledge (Wiki E2E):**`` with the
   provider's title and URL (platform-rendered, US-PK3), and the provider
   received a bearer-authenticated query containing the issue key and none of
   the diff text (US-PK2).
3. The provider is stopped. PR #3 is reviewed: the review is still posted and
   no failure is surfaced (US-PK5); only the citation block is absent.

The Admin UI assertions (disclosure literal in the built bundle, panel
endpoints) live in ``tests/e2e/test_admin_ui.py`` (Phase 8).
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

import httpx
import pytest

from adapter.product_knowledge.testing.fake_provider import Era, FakeProvider
from tests.e2e.conftest import (
    base_compose_env,
    compose,
    compose_down,
    wait_for_healthy,
    wait_for_log_line,
)

STUB_GITHUB_PORT = 19083
STUB_LLM_PORT = 19236
PROVIDER_PORT = 19237
_OWNER_REPO = "acme/product-knowledge-e2e"
_INSTALLATION_ID = 987655
_WEBHOOK_SECRET = "e2e-test-webhook-secret"
_WAIT_TIMEOUT_SECONDS = 120.0
_PROVIDER_TOKEN = "e2e-provider-token-canary"
_CREDENTIAL_REF = "e2e-wiki"
_DISPLAY_NAME = "Wiki E2E"
_ISSUE_KEY = "HR-77"
_DIFF_CANARY = "return 2;"  # appears only in the diff; must never reach the provider

_HELPER_BLOB_SHA = "a" * 40
_HELPER_TS = b"export function helper(): number {\n  return 2;\n}\n"
_PATCH = "@@ -1,3 +1,3 @@\n export function helper(): number {\n-  return 1;\n+  return 2;\n }\n"


# ---------------------------------------------------------------------------
# Fake provider as a host process (own thread + event loop, stoppable).
# ---------------------------------------------------------------------------


class _ProviderProcess:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._stop = threading.Event()
        self._started = threading.Event()
        self._error: BaseException | None = None
        self.provider = FakeProvider(
            era=Era.MODERN,
            token=_PROVIDER_TOKEN,
            bind_host="0.0.0.0",
            bind_port=PROVIDER_PORT,
        )
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def main() -> None:
            try:
                async with self.provider:
                    self._started.set()
                    while not self._stop.is_set():
                        await asyncio.sleep(0.1)
            except BaseException as exc:  # surfaced by start()
                self._error = exc
                self._started.set()

        self._loop.run_until_complete(main())

    def start(self) -> None:
        self._thread.start()
        assert self._started.wait(30), "fake provider did not start"
        assert self._error is None, f"fake provider failed to start: {self._error!r}"

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=15)


@pytest.fixture(scope="module")
def provider_process() -> Iterator[_ProviderProcess]:
    process = _ProviderProcess()
    process.start()
    try:
        yield process
    finally:
        process.stop()


# ---------------------------------------------------------------------------
# Stub GitHub / stub LLM (self-contained, same shape as test_context_builder).
# ---------------------------------------------------------------------------


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
                201, {"token": "ghs_e2e_pk_token", "expires_at": "2999-01-01T00:00:00Z"}
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
        elif re.search(r"/git/trees/", path):
            # The diff fetcher requires every changed file to have a tree entry.
            entry = {
                "type": "blob",
                "path": "helper.ts",
                "mode": "100644",
                "sha": _HELPER_BLOB_SHA,
                "size": len(_HELPER_TS),
            }
            self._send_json(200, {"tree": [entry], "truncated": False})
        elif re.search(r"/git/blobs/", path):
            content = base64.b64encode(_HELPER_TS).decode("ascii")
            self._send_json(200, {"content": content, "encoding": "base64"})
        else:
            self._send_json(404, {"message": "not found (stub)"})


class _StubLLMHandler(BaseHTTPRequestHandler):
    """
    Routes by call position within a job.

    1 = intent_summary, 2 = review_generation (one finding citing context id 0), 3 = verification.
    """

    _CONTENT = (
        "This PR changes a function's return value.",
        json.dumps(
            {
                "findings": [
                    {
                        "file": "helper.ts",
                        "line": 2,
                        "severity": "medium",
                        "message": "Changing this return value may break billing retries.",
                        "context_ref_ids": [0],
                    }
                ]
            }
        ),
        json.dumps(
            {
                "verdicts": [
                    {
                        "index": 0,
                        "valid": True,
                        "actionable": True,
                        "specific": True,
                        "valuable": True,
                    }
                ]
            }
        ),
    )

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request_body = self.rfile.read(length).decode("utf-8", "replace")
        call_number = getattr(self.server, "call_count", 0) + 1
        self.server.call_count = call_number  # type: ignore[attr-defined]
        prompts = getattr(self.server, "prompts", None)
        if isinstance(prompts, list):
            prompts.append(request_body)
        content = self._CONTENT[min(call_number, 3) - 1]
        body = json.dumps(
            {
                "id": "chatcmpl-e2e-pk-stub",
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


def _serve(handler: type[BaseHTTPRequestHandler], port: int) -> Iterator[HTTPServer]:
    server = HTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def stub_github_server() -> Iterator[HTTPServer]:
    gen = _serve(_StubGitHubHandler, STUB_GITHUB_PORT)
    server = next(gen)
    server.calls = _CallLog()  # type: ignore[attr-defined]
    try:
        yield server
    finally:
        next(gen, None)


@pytest.fixture(scope="module")
def stub_llm_server_pk() -> Iterator[HTTPServer]:
    gen = _serve(_StubLLMHandler, STUB_LLM_PORT)
    server = next(gen)
    server.call_count = 0  # type: ignore[attr-defined]
    server.prompts = []  # type: ignore[attr-defined]
    try:
        yield server
    finally:
        next(gen, None)


# ---------------------------------------------------------------------------
# Compose stack: provider first, credential map in env, then `up`.
# ---------------------------------------------------------------------------


def _e2e_env() -> dict[str, str]:
    credentials = json.dumps(
        {_CREDENTIAL_REF: {"token": _PROVIDER_TOKEN, "allowed_hosts": ["host.docker.internal"]}}
    )
    return base_compose_env(
        REVIEW_WORKER_LLM_ENDPOINT=f"http://host.docker.internal:{STUB_LLM_PORT}/v1",
        REVIEW_WORKER_GITHUB_API_BASE_URL=f"http://host.docker.internal:{STUB_GITHUB_PORT}",
        REVIEW_WORKER_POLL_INTERVAL_SECONDS="0.2",
        REVIEW_WORKER_PRODUCT_KNOWLEDGE_CREDENTIALS=credentials,
        REVIEW_ADMIN_API_PRODUCT_KNOWLEDGE_CREDENTIALS=credentials,
    )


@pytest.fixture(scope="module")
def running_stack(
    provider_process: _ProviderProcess,
    stub_github_server: HTTPServer,
    stub_llm_server_pk: HTTPServer,
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
        wait_for_healthy("admin_api", env)
        wait_for_log_line("worker", env, "worker started")
        yield env
    finally:
        print(compose("logs", "--no-color", env=env).stdout)
        compose_down(env)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _pull_request_payload(*, number: int, head_sha: str, title: str, head_ref: str) -> bytes:
    return json.dumps(
        {
            "installation": {"id": _INSTALLATION_ID},
            "repository": {"full_name": _OWNER_REPO, "private": True},
            "action": "opened",
            "pull_request": {
                "number": number,
                "title": title,
                "head": {
                    "sha": head_sha,
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


def _review_and_wait(
    env: dict[str, str],
    github: HTTPServer,
    llm: HTTPServer,
    *,
    number: int,
    title: str,
    head_ref: str,
) -> str:
    """Posts one PR webhook and returns the single review body posted for it."""
    calls: _CallLog = github.calls  # type: ignore[attr-defined]
    already = len(calls.check_run_bodies)
    # Each job makes three LLM calls in a fixed order; the startup probe has
    # already consumed one, so reset the position counter before every job.
    llm.call_count = 0  # type: ignore[attr-defined]
    status = _post_pull_request(
        env,
        _pull_request_payload(
            number=number, head_sha=f"{number:040x}", title=title, head_ref=head_ref
        ),
    )
    assert status == 200
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if len(calls.check_run_bodies) > already:
            return json.dumps(calls.review_bodies[-1])
        time.sleep(0.5)
    raise AssertionError(f"no check-run posted for PR #{number} within {_WAIT_TIMEOUT_SECONDS}s")


def _repo_scope(client: httpx.Client) -> str:
    tree = client.get("/api/v1/config").json()
    for org in tree["orgs"]:
        for repo in org["repos"]:
            if _OWNER_REPO.split("/")[1] in repo["name"] or repo["name"] == _OWNER_REPO:
                scope = repo["scope"]
                assert isinstance(scope, str)
                return scope
    raise AssertionError(f"repo {_OWNER_REPO} not in config tree: {tree}")


def _configure_repo_provider(env: dict[str, str]) -> None:
    with httpx.Client(base_url=f"http://localhost:{env['ADMIN_API_PORT']}") as client:
        login = client.post(
            "/api/v1/auth/login",
            json={
                "username": env["REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_USERNAME"],
                "password": env["REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_PASSWORD"],
            },
        )
        assert login.status_code == 200
        csrf = {"X-CSRF-Token": client.cookies["review_admin_csrf"]}
        put = client.put(
            f"/api/v1/config/{_repo_scope(client)}/product_knowledge_provider",
            json={
                "value": {
                    "enabled": True,
                    "display_name": _DISPLAY_NAME,
                    "endpoint_url": f"http://host.docker.internal:{PROVIDER_PORT}/mcp",
                    "credential_ref": _CREDENTIAL_REF,
                    "allow_public_repos": False,
                    "issue_key_prefixes": [],
                }
            },
            headers=csrf,
        )
        assert put.status_code == 200, put.text
        # The token never appears in any config response.
        assert _PROVIDER_TOKEN not in client.get("/api/v1/config").text


def _assert_provider_saw_scoped_authenticated_search(
    provider: FakeProvider, llm: HTTPServer, cited: str
) -> None:
    searches = [r for r in provider.requests if r.tool == "product_knowledge_search"]
    assert searches, "the provider never received a search"
    for request in provider.requests:
        assert request.authorization == f"Bearer {_PROVIDER_TOKEN}"
    query = str(searches[0].arguments["query"] if searches[0].arguments else "")
    assert _ISSUE_KEY in query
    assert _DIFF_CANARY not in query

    # The provider token never reaches the LLM prompt or the posted comment.
    assert _PROVIDER_TOKEN not in cited
    assert all(_PROVIDER_TOKEN not in prompt for prompt in llm.prompts)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The scenario.
# ---------------------------------------------------------------------------


def test_product_knowledge_is_cited_when_configured_and_review_survives_provider_down(
    running_stack: dict[str, str],
    provider_process: _ProviderProcess,
    stub_github_server: HTTPServer,
    stub_llm_server_pk: HTTPServer,
) -> None:
    env = running_stack
    provider = provider_process.provider

    # 1. No provider configured: review posted, no block, provider untouched.
    baseline = _review_and_wait(
        env,
        stub_github_server,
        stub_llm_server_pk,
        number=1,
        title="Tidy helper",
        head_ref="chore/tidy",
    )
    assert "Changing this return value" in baseline
    assert "Product knowledge" not in baseline
    assert provider.requests == []

    # 2. Configure the provider at repo scope through the real Admin API.
    _configure_repo_provider(env)

    cited = _review_and_wait(
        env,
        stub_github_server,
        stub_llm_server_pk,
        number=2,
        title=f"Fix billing retry {_ISSUE_KEY}",
        head_ref="bugfix/billing-retry",
    )
    assert f"**Product knowledge ({_DISPLAY_NAME}):**" in cited
    assert "Product doc 1" in cited
    assert "https://wiki.example.com/docs/1" in cited

    _assert_provider_saw_scoped_authenticated_search(provider, stub_llm_server_pk, cited)

    # 3. Provider down: the review is still posted; only the block is absent.
    provider_process.stop()
    degraded = _review_and_wait(
        env,
        stub_github_server,
        stub_llm_server_pk,
        number=3,
        title=f"Another change {_ISSUE_KEY}",
        head_ref="bugfix/another",
    )
    assert "Changing this return value" in degraded
    assert "Product knowledge" not in degraded
