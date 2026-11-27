"""llmgateway.models — pydantic models exchanged across the `LLMClient`
Protocol boundary (see `llmgateway/client.py`).

Per docs/rules/architecture.md's "validate every boundary" rule, nothing
past `adapter/llm` ever sees a raw provider dict/response object — every
call in and result out is one of these pydantic models. This module has
zero infra imports (no `litellm`, no `httpx`), matching the architecture
rule that domain packages stay free of adapter dependencies.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    """One turn in a chat-style completion request."""

    role: Literal["system", "user", "assistant"]
    content: str


class CompletionRequest(BaseModel):
    """A single completion request, provider-agnostic.

    `model` is the model identifier as configured for whichever endpoint
    the `LLMClient` implementation is wired to (e.g. the model currently
    loaded in LM Studio) — this package has no opinion on provider-specific
    naming/prefixing conventions; that translation is `adapter/llm`'s job.
    """

    model: str
    messages: list[Message] = Field(min_length=1)
    max_tokens: int | None = None
    temperature: float | None = None


class TokenUsage(BaseModel):
    """Token accounting for one completion, when the provider reports it."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class CompletionResponse(BaseModel):
    """The result of a completion request, normalized to a stable shape
    regardless of which provider served it.
    """

    content: str
    model: str
    finish_reason: str | None = None
    usage: TokenUsage | None = None
