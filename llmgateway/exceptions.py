"""Typed exceptions for llmgateway.

Per docs/rules/architecture.md's "specific exception types per domain
concern" rule: every `LLMClient` implementation (see `adapter/llm`) must
translate whatever raw provider/transport exception it hits (a raw
`litellm`/`httpx`/provider-SDK exception) into one of these before it
crosses back into `llmgateway` — mirrors `adapter/storage/exceptions.py`'s
`StorageError` pattern for the pgvector startup check.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base class for every error an `LLMClient` implementation raises."""


class LLMUnreachableError(LLMError):
    """Raised when the configured LLM endpoint could not be reached at
    all — connection refused, DNS failure, or the call timed out.

    Per `testing.md`, this is the first external-I/O failure mode this
    plan calls out explicitly ("LLM timeout" as a failure mode that
    matters); the startup connectivity check (`llmgateway/startup_check.py`)
    treats this as fatal and exits the Worker process non-zero.
    """

    def __init__(self, *, endpoint: str, detail: str) -> None:
        super().__init__(f"Could not reach the configured LLM endpoint ({endpoint}): {detail}")
        self.endpoint = endpoint
        self.detail = detail


class LLMMalformedResponseError(LLMError):
    """Raised when the configured LLM endpoint responded, but the response
    did not match the shape this gateway expects (invalid JSON, missing
    required fields, an empty choice list, ...).

    Distinct from `LLMUnreachableError` because the failure mode is
    different in practice: the endpoint is up and answering, but something
    about the provider/response is wrong (misconfigured model, a
    non-OpenAI-compatible server behind the configured URL, a provider-side
    bug) — worth surfacing differently to an operator than "unreachable."
    """

    def __init__(self, *, endpoint: str, detail: str) -> None:
        super().__init__(
            f"The configured LLM endpoint ({endpoint}) returned a malformed response: {detail}"
        )
        self.endpoint = endpoint
        self.detail = detail
