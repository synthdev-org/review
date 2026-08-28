"""Shared integration-test fixtures for adapter.llm.

Per docs/rules/testing.md's middle tier ("the LLM gateway against a
recorded LiteLLM cassette or a local LM Studio instance") and the plan's
Phase 3 instruction to test against "a recorded cassette or a stubbed HTTP
transport" rather than a live LM Studio instance (not available in CI):
these fixtures stand up a minimal, real HTTP server on `127.0.0.1` that
plays back a fixed response — the "cassette" — for whatever a test's
`respond` callable returns. This exercises the genuine
`litellm -> httpx -> wire` round trip, not a mock of litellm's internals,
while making no real network egress.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ResponseFactory = Callable[[], tuple[int, bytes]]
StartStub = Callable[[ResponseFactory], str]


def _make_handler(respond: ResponseFactory) -> type[BaseHTTPRequestHandler]:
    class _StubHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            status, body = respond()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass  # keep test output clean; failures surface via assertions

    return _StubHandler


@pytest.fixture
def stub_llm_server() -> Iterator[StartStub]:
    """Yields a `start(respond)` function: give it a zero-arg callable
    returning `(status_code, body_bytes)`, get back the base URL
    (`http://127.0.0.1:<port>/v1`) a `LiteLLMClient` can be pointed at.
    Every server started this way is shut down at the end of the test.
    """
    servers: list[HTTPServer] = []

    def start(respond: ResponseFactory) -> str:
        server = HTTPServer(("127.0.0.1", 0), _make_handler(respond))
        servers.append(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        return f"http://127.0.0.1:{port}/v1"

    yield start

    for server in servers:
        server.shutdown()
        server.server_close()


def unused_port_endpoint() -> str:
    """A `http://127.0.0.1:<port>/v1` URL for a port nothing is listening
    on, for exercising a genuine connection-refused failure — no server is
    ever started on it. Found by binding then immediately releasing a port
    the OS just handed out, rather than a hardcoded number that might
    already be in use on some CI runner."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def canned_completion_json(*, content: str = "pong", model: str = "local-model") -> bytes:
    """A well-formed OpenAI-style `/v1/chat/completions` response body —
    the recorded-cassette content for the happy-path integration test."""
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    return json.dumps(payload).encode()
