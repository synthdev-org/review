"""Integration tests for the request-capture fixtures in adapter/llm/conftest.py (Phase 1b of
docs/plans/plan-local-mode.md).
"""

from __future__ import annotations

import pytest

from adapter.llm.conftest import (
    CapturedRequest,
    StartCapturingStub,
    StartStub,
    canned_completion_json,
)
from adapter.llm.litellm_client import LiteLLMClient
from llmgateway.models import CompletionRequest, Message

pytestmark = pytest.mark.integration


def _request(text: str) -> CompletionRequest:
    return CompletionRequest(
        model="local-model",
        messages=[Message(role="system", content="be brief"), Message(role="user", content=text)],
    )


@pytest.mark.asyncio
async def test_stub_records_each_request_and_lets_the_reply_depend_on_it(
    stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    endpoint = stub_llm_capture(
        lambda request: (200, canned_completion_json(content=f"saw {len(request.prompt_text())}"))
    )
    client = LiteLLMClient(endpoint=endpoint, api_key="not-needed")

    assert llm_requests == []
    first = await client.complete(_request("hello"), timeout=10)
    second = await client.complete(_request("a longer question"), timeout=10)

    assert [r.path for r in llm_requests] == ["/v1/chat/completions"] * 2
    assert "hello" in llm_requests[0].prompt_text()
    assert "a longer question" in llm_requests[1].prompt_text()
    assert llm_requests[0].json()["model"] == "local-model"
    assert first.content != second.content


@pytest.mark.asyncio
async def test_the_fixed_cassette_stub_records_too(
    stub_llm_server: StartStub, llm_requests: list[CapturedRequest]
) -> None:
    endpoint = stub_llm_server(lambda: (200, canned_completion_json(content="pong")))

    await LiteLLMClient(endpoint=endpoint, api_key="not-needed").complete(
        _request("ping"), timeout=10
    )

    assert len(llm_requests) == 1
    assert "ping" in llm_requests[0].prompt_text()
