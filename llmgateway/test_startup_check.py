"""Unit tests for llmgateway.startup_check.

Per docs/rules/testing.md's unit tier: exercised against a fake
implementation of the consumer-defined `LLMClient` Protocol, not a real
adapter — no `litellm`/`httpx` import anywhere in this file.
"""

from __future__ import annotations

import pytest

from llmgateway.client import LLMClient
from llmgateway.exceptions import LLMMalformedResponseError, LLMUnreachableError
from llmgateway.models import CompletionRequest, CompletionResponse
from llmgateway.startup_check import check_llm_reachable


class _RecordingFakeClient:
    """A fake `LLMClient` that records the request/timeout it was called
    with and returns (or raises) whatever the test configures."""

    def __init__(
        self, *, response: CompletionResponse | None = None, error: Exception | None = None
    ):
        self._response = response
        self._error = error
        self.calls: list[tuple[CompletionRequest, float]] = []

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        self.calls.append((request, timeout))
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


def _assert_is_llm_client(client: object) -> None:
    """mypy already enforces Protocol conformance at type-check time; this
    runtime assertion just documents that `_RecordingFakeClient` is meant
    to satisfy `LLMClient`, not merely resemble it."""
    assert isinstance(client, LLMClient)


@pytest.mark.asyncio
async def test_check_llm_reachable_calls_complete_with_model_and_timeout() -> None:
    fake = _RecordingFakeClient(response=CompletionResponse(content="pong", model="local-model"))
    _assert_is_llm_client(fake)

    await check_llm_reachable(fake, model="local-model", timeout=7.5)

    assert len(fake.calls) == 1
    request, timeout = fake.calls[0]
    assert timeout == 7.5
    assert request.model == "local-model"
    assert request.max_tokens == 1
    assert len(request.messages) == 1
    assert request.messages[0].content


@pytest.mark.asyncio
async def test_check_llm_reachable_propagates_unreachable_error() -> None:
    fake = _RecordingFakeClient(
        error=LLMUnreachableError(endpoint="http://127.0.0.1:1", detail="connection refused")
    )

    with pytest.raises(LLMUnreachableError):
        await check_llm_reachable(fake, model="local-model", timeout=5.0)


@pytest.mark.asyncio
async def test_check_llm_reachable_propagates_malformed_response_error() -> None:
    fake = _RecordingFakeClient(
        error=LLMMalformedResponseError(endpoint="http://127.0.0.1:1234", detail="no choices")
    )

    with pytest.raises(LLMMalformedResponseError):
        await check_llm_reachable(fake, model="local-model", timeout=5.0)
