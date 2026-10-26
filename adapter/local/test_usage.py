"""Unit tests for the token-usage recorder, with a fake client at the LLMClient boundary."""

from __future__ import annotations

import pytest

from adapter.local.usage import UsageRecordingClient
from llmgateway.models import CompletionRequest, CompletionResponse, Message, TokenUsage


class _FakeClient:
    def __init__(self, usages: list[TokenUsage | None]) -> None:
        self._usages = list(usages)

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        return CompletionResponse(content="ok", model="m", usage=self._usages.pop(0))


def _request() -> CompletionRequest:
    return CompletionRequest(model="m", messages=[Message(role="user", content="hi")])


@pytest.mark.asyncio
async def test_usage_is_summed_across_requests() -> None:
    client = UsageRecordingClient(
        _FakeClient(
            [
                TokenUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
                TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            ]
        )
    )

    await client.complete(_request(), timeout=1)
    await client.complete(_request(), timeout=1)

    usage = client.usage()
    assert usage is not None
    assert (usage.requests, usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (
        2,
        11,
        3,
        14,
    )


@pytest.mark.asyncio
async def test_no_reported_usage_is_none_not_zero() -> None:
    client = UsageRecordingClient(_FakeClient([None]))

    response = await client.complete(_request(), timeout=1)

    assert response.content == "ok"
    assert client.usage() is None
