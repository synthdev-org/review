"""Integration tests for adapter.llm.litellm_client.LiteLLMClient.

Per docs/rules/testing.md's middle tier and the plan's Phase 3 Tests
section: exercises the real `litellm -> httpx -> wire` round trip against a
local stub HTTP server standing in for LM Studio's OpenAI-compatible
endpoint (a recorded cassette, not a mock of litellm's internals) plus a
genuine connection-refused case. No real network egress — everything binds
to `127.0.0.1`, so this needs no live LM Studio instance and no external
network access.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from adapter.llm.conftest import StartStub, canned_completion_json, unused_port_endpoint
from adapter.llm.litellm_client import LiteLLMClient
from llmgateway.exceptions import LLMMalformedResponseError, LLMUnreachableError
from llmgateway.models import CompletionRequest, Message

pytestmark = pytest.mark.integration


def _request(model: str = "local-model") -> CompletionRequest:
    return CompletionRequest(model=model, messages=[Message(role="user", content="ping")])


def _extra(record: logging.LogRecord, key: str) -> object:
    """`logging.LogRecord` accepts arbitrary attributes via the `extra=`
    kwarg at log-call time, so mypy --strict has no static knowledge of them
    — read via `__dict__` (typed `dict[str, Any]` on `LogRecord`) rather than
    attribute access, which `attr-defined` would reject."""
    return record.__dict__[key]


@pytest.mark.asyncio
async def test_complete_returns_parsed_response_from_recorded_cassette(
    stub_llm_server: StartStub,
) -> None:
    endpoint = stub_llm_server(lambda: (200, canned_completion_json(content="pong")))
    client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")

    response = await client.complete(_request(), timeout=5.0)

    assert response.content == "pong"
    assert response.model == "local-model"
    assert response.finish_reason == "stop"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 5
    assert response.usage.completion_tokens == 3
    assert response.usage.total_tokens == 8


@pytest.mark.asyncio
async def test_complete_raises_unreachable_error_when_connection_refused() -> None:
    # No server is ever started on this port — a genuine connection-refused,
    # exactly the "LM Studio isn't running" failure mode this test exists to
    # prove is surfaced as a typed error rather than a raw litellm/httpx one.
    endpoint = unused_port_endpoint()
    client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")

    with pytest.raises(LLMUnreachableError) as excinfo:
        await client.complete(_request(), timeout=3.0)

    assert excinfo.value.endpoint == endpoint
    assert "not-needed" not in str(excinfo.value)  # the api key never leaks into the error


@pytest.mark.asyncio
async def test_complete_raises_unreachable_error_on_timeout(stub_llm_server: StartStub) -> None:
    def hang() -> tuple[int, bytes]:
        time.sleep(5)
        return 200, canned_completion_json()

    endpoint = stub_llm_server(hang)
    client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")

    with pytest.raises(LLMUnreachableError):
        await client.complete(_request(), timeout=0.5)


@pytest.mark.asyncio
async def test_complete_raises_malformed_response_error_on_invalid_json(
    stub_llm_server: StartStub,
) -> None:
    endpoint = stub_llm_server(lambda: (200, b"not json at all {{{"))
    client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")

    with pytest.raises(LLMMalformedResponseError) as excinfo:
        await client.complete(_request(), timeout=5.0)

    assert excinfo.value.endpoint == endpoint


@pytest.mark.asyncio
async def test_complete_raises_malformed_response_error_on_missing_choices(
    stub_llm_server: StartStub,
) -> None:
    body = json.dumps({"id": "x", "object": "chat.completion", "model": "local-model"}).encode()
    endpoint = stub_llm_server(lambda: (200, body))
    client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")

    with pytest.raises(LLMMalformedResponseError):
        await client.complete(_request(), timeout=5.0)


@pytest.mark.asyncio
async def test_complete_logs_request_and_success_with_latency_and_model(
    stub_llm_server: StartStub, caplog: pytest.LogCaptureFixture
) -> None:
    """Proves the structured logging around the LiteLLM call boundary
    (Phase 3's Observability & rollback section) actually carries the
    fields later pipeline stages' cost/latency visibility depends on — a
    regression that dropped `latency_seconds` or `llm_model` from the log
    call would fail this test, not just look fine in a docstring."""
    endpoint = stub_llm_server(lambda: (200, canned_completion_json(content="pong")))
    client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")

    with caplog.at_level(logging.INFO, logger="adapter.llm.litellm_client"):
        await client.complete(_request(model="local-model"), timeout=5.0)

    requested = next(r for r in caplog.records if r.message == "llm completion requested")
    assert _extra(requested, "llm_endpoint") == endpoint
    assert _extra(requested, "llm_model") == "local-model"
    assert _extra(requested, "timeout") == 5.0

    succeeded = next(r for r in caplog.records if r.message == "llm completion succeeded")
    assert _extra(succeeded, "llm_endpoint") == endpoint
    assert _extra(succeeded, "llm_model") == "local-model"
    latency = _extra(succeeded, "latency_seconds")
    assert isinstance(latency, float)
    assert latency >= 0.0


@pytest.mark.asyncio
async def test_complete_logs_failure_with_error_class_and_latency(
    caplog: pytest.LogCaptureFixture,
) -> None:
    endpoint = unused_port_endpoint()
    client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")

    with (
        caplog.at_level(logging.INFO, logger="adapter.llm.litellm_client"),
        pytest.raises(LLMUnreachableError),
    ):
        await client.complete(_request(), timeout=3.0)

    failed = next(r for r in caplog.records if r.message == "llm completion failed")
    assert _extra(failed, "llm_endpoint") == endpoint
    assert _extra(failed, "error_class") == "LLMUnreachableError"
    latency = _extra(failed, "latency_seconds")
    assert isinstance(latency, float)
    assert latency >= 0.0
