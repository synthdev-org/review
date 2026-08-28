"""Tests for the worker entrypoint's main() composition root.

`test_main_parses_settings_and_logs_startup` is the Phase 1 unit test
(config parsing + logging only), kept a pure unit test — no real I/O — by
substituting a fake `LLMClient` at `_build_llm_client`, the one seam
`main()` exposes for it (per docs/rules/architecture.md's "wire concrete
implementations only at the composition root" rule: the composition root
is exactly where a test substitutes a fake for the real adapter).

The two tests Phase 3 adds below exercise the LLM Gateway startup
connectivity check genuinely, through the real `LiteLLMClient`/`litellm`
code path against local-only HTTP servers, per docs/rules/testing.md's
middle tier; both are marked `@pytest.mark.integration` because they cross
the `adapter.llm` boundary, not merely call a fake.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import apps.worker.main as worker_main
from apps.worker.main import main
from llmgateway.models import CompletionRequest, CompletionResponse


@pytest.fixture(autouse=True)
def _reset_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers[:] = original_handlers
    root.setLevel(original_level)


class _FakeLLMClient:
    """A fake `LLMClient` (see llmgateway.client.LLMClient) that answers
    every call immediately, with no real I/O — used to keep the plain
    "does main() parse config and log startup" test a pure unit test."""

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        return CompletionResponse(content="pong", model=request.model)


def test_main_parses_settings_and_logs_startup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    monkeypatch.setattr(worker_main, "_build_llm_client", lambda settings: _FakeLLMClient())

    main()

    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line]
    payloads = [json.loads(line) for line in lines]

    assert any(
        p["message"] == "worker starting" and p.get("environment") == "development"
        for p in payloads
    )
    assert any(p["message"] == "llm endpoint reachable" for p in payloads)
    assert any(p["message"] == "worker idle: no job dispatch wired yet" for p in payloads)


def _unused_port_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def _start_stub_llm_server() -> HTTPServer:
    """A minimal local HTTP server standing in for LM Studio's
    OpenAI-compatible endpoint, returning one well-formed completion for
    every request. Kept small and inline here rather than imported from
    `adapter/llm/conftest.py` — `apps/worker`'s tests stay self-contained,
    the same way `adapter/storage`'s do."""
    payload = {
        "id": "chatcmpl-test",
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
    body = json.dumps(payload).encode()

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.integration
def test_main_succeeds_when_llm_endpoint_reachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    server = _start_stub_llm_server()
    port = server.server_address[1]
    monkeypatch.setenv("REVIEW_WORKER_LLM_ENDPOINT", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setenv("REVIEW_WORKER_LLM_TIMEOUT_SECONDS", "5")

    try:
        main()  # must not raise SystemExit
    finally:
        server.shutdown()
        server.server_close()

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert any(p["message"] == "llm endpoint reachable" for p in payloads)
    assert any(p["message"] == "worker idle: no job dispatch wired yet" for p in payloads)


@pytest.mark.integration
def test_main_exits_non_zero_when_llm_endpoint_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_WORKER_ENVIRONMENT", "development")
    # Nothing is listening on this port — a genuine connection-refused,
    # exactly the "LM Studio isn't running" case this phase's fail-fast
    # startup check must catch.
    monkeypatch.setenv("REVIEW_WORKER_LLM_ENDPOINT", _unused_port_endpoint())
    monkeypatch.setenv("REVIEW_WORKER_LLM_TIMEOUT_SECONDS", "2")

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.out.strip().splitlines() if line]
    assert any(
        p["message"] == "LLM endpoint unreachable at startup; exiting" and p["level"] == "ERROR"
        for p in payloads
    )
