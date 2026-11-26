"""llmgateway.client — the `LLMClient` Protocol.

Per docs/rules/architecture.md's Decoupling section ("Anything with >1 real
or clearly upcoming implementation ... sits behind a stable
`typing.Protocol`, defined at the consumer"): this Protocol is defined here, in the
domain package that *consumes* an LLM provider, not in `adapter/llm` that
implements it. `adapter/llm.LiteLLMClient` is the only implementation today;
swapping it for a different one (or adding a second, e.g. a direct
Anthropic-SDK-backed client) needs zero changes here or to any future
caller typed against this Protocol.

This module imports nothing beyond the standard library and
`llmgateway.models` — no `litellm`, no `httpx` — which is what keeps
`llmgateway/` itself free of infra imports per the architecture rule.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from llmgateway.models import CompletionRequest, CompletionResponse


@runtime_checkable
class LLMClient(Protocol):
    """A provider-agnostic completion call.

    Every implementation must:

    - Accept an explicit `timeout` (seconds) on every call and honor it —
      per architecture.md's "every I/O call takes a timeout and respects
      cancellation" rule. There is no default: a caller that forgets to
      pass one is a bug this Protocol's signature makes impossible to miss.
    - Raise `llmgateway.exceptions.LLMUnreachableError` or
      `llmgateway.exceptions.LLMMalformedResponseError` (never a raw
      provider/transport exception) on failure.
    """

    async def complete(
        self, request: CompletionRequest, *, timeout: float
    ) -> CompletionResponse: ...
