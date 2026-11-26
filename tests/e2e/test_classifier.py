"""
E2E scenario for the classifier setting.

The classifier setting absent, then ``shadow``, ``active``, a 401 and a provider outage, over
the real Docker Compose stack (Webhook API + Worker + Admin API + Postgres), one synthetic
webhook per scenario, a stub GitHub and a stub LLM (reused from ``test_product_knowledge``).

The classifier is a *host process* serving TLS (the setting must be ``https``). Its
certificate is generated per run and is the Worker's only trust anchor: a Compose override
bind-mounts it over the Worker's system CA bundle (the egress guard uses the system trust
store and ignores ``SSL_CERT_*``). ``REVIEW_WORKER_CLASSIFIER_ALLOW_PRIVATE_NETWORKS=true``
lets the Worker reach ``host.docker.internal``, which resolves to the Docker host gateway.
The real endpoint is never contacted: no TypeSafe key exists for CI.

Per scenario, the stub LLM's call count shows who decided: 3 calls (summary, review,
verification) means the LLM judge verified; 2 means the classifier decided alone.
"""

from __future__ import annotations

import json
import ssl
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from adapter.product_knowledge.testing.loopback import self_signed_cert
from findings.markdown import escape_markdown
from tests.e2e.conftest import (
    COMPOSE_FILE,
    PROJECT_NAME,
    REPO_ROOT,
    base_compose_env,
    compose,
    compose_down,
    wait_for_healthy,
    wait_for_log_line,
)
from tests.e2e.test_product_knowledge import (
    _OWNER_REPO,
    STUB_GITHUB_PORT,
    STUB_LLM_PORT,
    _CallLog,
    _review_and_wait,
    _serve,
    _StubGitHubHandler,
    _StubLLMHandler,
)

CLASSIFIER_PORT = 19443
_TOKEN = "e2e-classifier-token-canary"
_REF = "e2e-classifier"
_HOST = "host.docker.internal"
_FINDING = "Changing this return value may break billing retries."
# The posted comment markdown-escapes the message, and `body` below is JSON-encoded.
_POSTED_FINDING = json.dumps(escape_markdown(_FINDING))[1:-1]


@dataclass
class _Seen:
    authorizations: list[str] = field(default_factory=list)
    bodies: list[dict[str, object]] = field(default_factory=list)


class _ClassifierHandler(BaseHTTPRequestHandler):
    """``POST /v1/systemone``: all-yes noul answers, or a 401 when ``server.mode == "401"``."""

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        seen = getattr(self.server, "seen", None)
        assert isinstance(seen, _Seen)
        body: dict[str, object] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.authorizations.append(self.headers.get("Authorization", ""))
        seen.bodies.append(body)
        if getattr(self.server, "mode", "ok") == "401":
            self.send_response(401)
            self.end_headers()
            return
        questions = body["questions"]
        assert isinstance(questions, dict)
        payload = json.dumps(
            {
                "model": body["model"],
                "answers": {qid: {"type": "noul", "noul": 0.95} for qid in questions},
                "usage": {"input_tokens": 10, "output_tokens": 1},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture(scope="module")
def tls_material(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    """``(cert, key, ca_bundle)``: a throwaway self-signed cert for the stub, its own CA."""
    cert_pem, key_pem = self_signed_cert(_HOST)
    directory = tmp_path_factory.mktemp("classifier-tls")
    cert, key, bundle = directory / "cert.pem", directory / "key.pem", directory / "ca.pem"
    cert.write_bytes(cert_pem)
    key.write_bytes(key_pem)
    bundle.write_bytes(cert_pem)
    return cert, key, bundle


@pytest.fixture(scope="module")
def classifier_server(tls_material: tuple[Path, Path, Path]) -> Iterator[HTTPServer]:
    cert, key, _ = tls_material
    server = HTTPServer(("0.0.0.0", CLASSIFIER_PORT), _ClassifierHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.seen = _Seen()  # type: ignore[attr-defined]
    server.mode = "ok"  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
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
def stub_llm() -> Iterator[HTTPServer]:
    gen = _serve(_StubLLMHandler, STUB_LLM_PORT)
    server = next(gen)
    server.call_count = 0  # type: ignore[attr-defined]
    server.prompts = []  # type: ignore[attr-defined]
    try:
        yield server
    finally:
        next(gen, None)


def _stub_url(port: int, path: str = "") -> str:
    """Plain-HTTP URL of a local stub server (no TLS on loopback stubs)."""
    return str(httpx.URL(scheme="http", host=_HOST, port=port)).rstrip("/") + path


def _env() -> dict[str, str]:
    credentials = json.dumps({_REF: {"token": _TOKEN, "allowed_hosts": [_HOST]}})
    return base_compose_env(
        REVIEW_WORKER_LLM_ENDPOINT=_stub_url(STUB_LLM_PORT, "/v1"),
        REVIEW_WORKER_GITHUB_API_BASE_URL=_stub_url(STUB_GITHUB_PORT),
        REVIEW_WORKER_POLL_INTERVAL_SECONDS="0.2",
        REVIEW_WORKER_PRODUCT_KNOWLEDGE_CREDENTIALS=credentials,
        REVIEW_ADMIN_API_PRODUCT_KNOWLEDGE_CREDENTIALS=credentials,
        REVIEW_WORKER_CLASSIFIER_ALLOW_PRIVATE_NETWORKS="true",
    )


@pytest.fixture(scope="module")
def running_stack(
    tls_material: tuple[Path, Path, Path],
    classifier_server: HTTPServer,
    stub_github_server: HTTPServer,
    stub_llm: HTTPServer,
) -> Iterator[dict[str, str]]:
    env = _env()
    override = Path(tempfile.mkdtemp()) / "classifier-ca.override.yml"
    override.write_text(
        "services:\n  worker:\n    volumes:\n"
        f"      - {tls_material[2]}:/etc/ssl/certs/ca-certificates.crt:ro\n"
    )
    compose_down(env)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            PROJECT_NAME,
            "-f",
            str(COMPOSE_FILE),
            "-f",
            str(override),
            "up",
            "-d",
            "--build",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, f"compose up failed.\n{result.stdout}\n{result.stderr}"
    try:
        wait_for_healthy("webhook_api", env)
        wait_for_healthy("admin_api", env)
        wait_for_log_line("worker", env, "worker started")
        yield env
    finally:
        print(compose("logs", "--no-color", env=env).stdout)
        compose_down(env)


def _set_classifier(env: dict[str, str], mode: str) -> None:
    with httpx.Client(base_url=f"http://localhost:{env['ADMIN_API_PORT']}") as client:
        login = client.post(
            "/api/v1/auth/login",
            json={
                "username": env["REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_USERNAME"],
                "password": env["REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_PASSWORD"],
            },
        )
        assert login.status_code == 200
        org_scope = next(
            org["scope"]
            for org in client.get("/api/v1/config").json()["orgs"]
            if any(repo["name"].endswith(_OWNER_REPO.split("/")[1]) for repo in org["repos"])
        )
        put = client.put(
            f"/api/v1/config/{org_scope}/classifier_provider",
            json={
                "value": {
                    "enabled": True,
                    "backend": "typesafe",
                    "endpoint_url": f"https://{_HOST}:{CLASSIFIER_PORT}",
                    "credential_ref": _REF,
                    "model": "jev-1.14.0",
                    "mode": mode,
                    "disclosure_acknowledged": True,
                }
            },
            headers={"X-CSRF-Token": client.cookies["review_admin_csrf"]},
        )
        assert put.status_code == 200, put.text
        assert _TOKEN not in client.get("/api/v1/config").text


def _review(env: dict[str, str], github: HTTPServer, llm: HTTPServer, number: int) -> int:
    """Reviews one PR; returns how many LLM calls the job made (3 = LLM judge verified)."""
    body = _review_and_wait(
        env, github, llm, number=number, title=f"Change {number}", head_ref=f"c/{number}"
    )
    assert _POSTED_FINDING in body, "the finding must be posted in every scenario"
    return int(llm.call_count)  # type: ignore[attr-defined]


def test_classifier_absent_shadow_active_401_and_outage(
    running_stack: dict[str, str],
    classifier_server: HTTPServer,
    stub_github_server: HTTPServer,
    stub_llm: HTTPServer,
) -> None:
    env = running_stack
    seen: _Seen = classifier_server.seen  # type: ignore[attr-defined]

    # 1. Setting absent: today's behaviour, the classifier host is never contacted.
    assert _review(env, stub_github_server, stub_llm, 1) == 3
    assert seen.bodies == []

    # 2. Shadow: the classifier is called (bearer token, never in the body), the LLM decides.
    _set_classifier(env, "shadow")
    assert _review(env, stub_github_server, stub_llm, 2) == 3
    assert seen.bodies
    assert set(seen.authorizations) == {f"Bearer {_TOKEN}"}
    assert _TOKEN not in json.dumps(seen.bodies)

    # 3. Active: confident "yes" answers decide alone, with no LLM verification call.
    _set_classifier(env, "active")
    assert _review(env, stub_github_server, stub_llm, 3) == 2

    # 4. 401 from the provider: the job still posts, the LLM judge takes over, one error logged.
    classifier_server.mode = "401"  # type: ignore[attr-defined]
    before = len(seen.bodies)
    assert _review(env, stub_github_server, stub_llm, 4) == 3
    assert len(seen.bodies) > before
    wait_for_log_line("worker", env, "classifier provider rejected the credential (HTTP 401)")

    # 5. Provider down: the job still posts through the LLM judge.
    classifier_server.shutdown()
    classifier_server.server_close()
    assert _review(env, stub_github_server, stub_llm, 5) == 3
