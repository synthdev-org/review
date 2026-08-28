"""Shared fixtures/helpers for the Phase 5 Docker Compose E2E tests.

Per docs/rules/testing.md's top tier: these tests drive the built container
images via the same docker-compose.yml a developer uses locally, over HTTP
against apps/webhook_api's actual entrypoint — no shortcuts that bypass it.

There is no real LM Studio in CI (or in most developers' environments), so
`stub_llm_server` below stands in for it: a minimal OpenAI-compatible HTTP
server bound to the exact host/port docker-compose.yml's `worker` service
points its `LLMClient` at by default (`host.docker.internal:1234`, per the
plan's Phase 5 instruction). `worker`'s `extra_hosts: host.docker.internal:
host-gateway` entry (docker-compose.yml) is what makes this reachable from
inside the container on Linux.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
PROJECT_NAME = "review_e2e"
STUB_LLM_PORT = 1234

# Generous but bounded: image builds on this host can be slow the first time
# (cold `uv sync` dependency download, and this host's Docker containers can
# see slow DNS resolution for public hostnames — a known Tailscale/Docker
# interaction, not a defect in the stack itself).
_COMPOSE_TIMEOUT_SECONDS = 900.0
_HEALTHY_TIMEOUT_SECONDS = 120.0


class _StubCompletionHandler(BaseHTTPRequestHandler):
    """Answers every POST with one well-formed OpenAI-style chat completion
    — enough for `llmgateway.startup_check.check_llm_reachable`'s one
    low-cost probe call (apps/worker/main.py) to succeed."""

    _BODY = json.dumps(
        {
            "id": "chatcmpl-e2e-stub",
            "object": "chat.completion",
            "created": 0,
            "model": "local-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "pong"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    ).encode()

    def do_POST(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self._BODY)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="package")
def stub_llm_server() -> Iterator[HTTPServer]:
    """Bind on every interface (not just loopback) so traffic arriving via
    Docker's `host-gateway` bridge reaches this handler, not just
    127.0.0.1-originated connections."""
    server = HTTPServer(("0.0.0.0", STUB_LLM_PORT), _StubCompletionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def base_compose_env(**overrides: str) -> dict[str, str]:
    """The environment `docker compose` substitutes `${VAR}` references
    from — real process env plus test-scoped defaults for every value
    docker-compose.yml requires or that would otherwise collide with a
    developer's own running stack (distinct ports, project name)."""
    env = dict(os.environ)
    env.update(
        {
            "POSTGRES_USER": "review",
            "POSTGRES_PASSWORD": "e2e-test-password",
            "POSTGRES_DB": "review",
            "POSTGRES_PORT": "15432",
            "WEBHOOK_API_PORT": "18000",
            "REVIEW_DATABASE_URL": (
                "postgresql+asyncpg://review:e2e-test-password@postgres:5432/review"
            ),
            "REVIEW_WEBHOOK_API_ENVIRONMENT": "development",
            "REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET": "e2e-test-webhook-secret",
            "REVIEW_WORKER_ENVIRONMENT": "development",
            "REVIEW_WORKER_LLM_ENDPOINT": f"http://host.docker.internal:{STUB_LLM_PORT}/v1",
            "REVIEW_WORKER_LLM_TIMEOUT_SECONDS": "10",
        }
    )
    env.update(overrides)
    return env


def compose(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run `docker compose -p review_e2e -f docker-compose.yml <args>`
    against a dedicated project name, so this never collides with a
    developer's own `docker compose up` of the same file."""
    return subprocess.run(
        ["docker", "compose", "-p", PROJECT_NAME, "-f", str(COMPOSE_FILE), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=_COMPOSE_TIMEOUT_SECONDS,
    )


def compose_down(env: dict[str, str]) -> None:
    """Best-effort teardown: removes containers, networks, and any volume
    left by a previous run — safe to call even if nothing is up."""
    compose("down", "--volumes", "--remove-orphans", env=env)


def container_id(service: str, env: dict[str, str], *, all_states: bool = False) -> str:
    args = ["ps", "-q", service]
    if all_states:
        args.insert(1, "-a")
    return compose(*args, env=env).stdout.strip()


def docker_inspect(container: str, fmt: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", "--format", fmt, container],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


def wait_for_healthy(
    service: str, env: dict[str, str], timeout: float = _HEALTHY_TIMEOUT_SECONDS
) -> None:
    deadline = time.monotonic() + timeout
    last_status = "no container"
    while time.monotonic() < deadline:
        cid = container_id(service, env)
        if cid:
            raw = docker_inspect(cid, "{{json .State.Health}}")
            health = json.loads(raw) if raw and raw != "null" else None
            last_status = health["Status"] if health else "no healthcheck reported yet"
            if health and health["Status"] == "healthy":
                return
        time.sleep(1)
    raise AssertionError(
        f"{service} did not become healthy within {timeout}s (last: {last_status})"
    )


def wait_for_exit(
    service: str, env: dict[str, str], timeout: float = _HEALTHY_TIMEOUT_SECONDS
) -> int:
    deadline = time.monotonic() + timeout
    last_status = "no container"
    while time.monotonic() < deadline:
        cid = container_id(service, env, all_states=True)
        if cid:
            status = docker_inspect(cid, "{{.State.Status}}")
            last_status = status
            if status == "exited":
                return int(docker_inspect(cid, "{{.State.ExitCode}}"))
        time.sleep(1)
    raise AssertionError(f"{service} did not exit within {timeout}s (last status: {last_status})")


def restart_count(service: str, env: dict[str, str]) -> int:
    cid = container_id(service, env, all_states=True)
    if not cid:
        raise AssertionError(f"no container found for service {service!r}")
    return int(docker_inspect(cid, "{{.RestartCount}}"))
