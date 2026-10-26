"""adapter.local.usage — an ``LLMClient`` decorator that totals the token usage the endpoint reports
for the run summary. It changes nothing about the request or the response."""

from __future__ import annotations

from adapter.local.summary import TokenUsageSummary
from llmgateway.client import LLMClient
from llmgateway.models import CompletionRequest, CompletionResponse


class UsageRecordingClient:
    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self._requests = 0
        self._prompt = 0
        self._completion = 0
        self._total = 0
        self._reported = False

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        response = await self._inner.complete(request, timeout=timeout)
        self._requests += 1
        if response.usage is not None:
            self._reported = True
            self._prompt += response.usage.prompt_tokens
            self._completion += response.usage.completion_tokens
            self._total += response.usage.total_tokens
        return response

    def usage(self) -> TokenUsageSummary | None:
        """``None`` unless the endpoint reported usage for at least one request."""
        if not self._reported:
            return None
        return TokenUsageSummary(
            requests=self._requests,
            prompt_tokens=self._prompt,
            completion_tokens=self._completion,
            total_tokens=self._total,
        )


_: type[LLMClient] = UsageRecordingClient
