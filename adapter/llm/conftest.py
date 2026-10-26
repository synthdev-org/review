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
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

ResponseFactory = Callable[[], tuple[int, bytes]]
StartStub = Callable[[ResponseFactory], str]


@dataclass(frozen=True)
class CapturedRequest:
    """One request the stub received: enough for a test to assert on what was (or was not) sent."""

    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)

    def prompt_text(self) -> str:
        """Every chat message's content joined, for substring assertions on what the model saw."""
        messages = self.json().get("messages", [])
        return "\n".join(str(m.get("content", "")) for m in messages)


RequestResponder = Callable[[CapturedRequest], tuple[int, bytes]]
StartCapturingStub = Callable[[RequestResponder], str]


def _make_handler(
    respond: RequestResponder, requests: list[CapturedRequest]
) -> type[BaseHTTPRequestHandler]:
    class _StubHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            request = CapturedRequest(
                path=self.path,
                headers={k.lower(): v for k, v in self.headers.items()},
                body=self.rfile.read(length),
            )
            requests.append(request)
            status, body = respond(request)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass  # keep test output clean; failures surface via assertions

    return _StubHandler


@pytest.fixture
def llm_requests() -> list[CapturedRequest]:
    """Every request any stub server of this test received, in arrival order. Assert on it for
    "the model was never called" (`== []`) and for what a prompt did or did not contain; without
    capture such assertions pass vacuously."""
    return []


@pytest.fixture
def stub_llm_capture(llm_requests: list[CapturedRequest]) -> Iterator[StartCapturingStub]:
    """Yields a `start(respond)` function: give it a callable taking the `CapturedRequest` and
    returning `(status_code, body_bytes)`, get back the base URL (`http://127.0.0.1:<port>/v1`)
    a `LiteLLMClient` can be pointed at. Every request is also appended to `llm_requests`.
    Every server started this way is shut down at the end of the test.
    """
    servers: list[HTTPServer] = []

    def start(respond: RequestResponder) -> str:
        server = HTTPServer(("127.0.0.1", 0), _make_handler(respond, llm_requests))
        servers.append(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        return f"http://127.0.0.1:{port}/v1"

    yield start

    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def stub_llm_server(stub_llm_capture: StartCapturingStub) -> StartStub:
    """Like `stub_llm_capture`, but `respond` is a zero-arg callable that never sees the request
    (a fixed "cassette"). Requests are still recorded in `llm_requests`."""

    def start(respond: ResponseFactory) -> str:
        return stub_llm_capture(lambda _request: respond())

    return start


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
