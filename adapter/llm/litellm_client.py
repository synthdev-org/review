"""adapter.llm.litellm_client — LiteLLM-backed implementation of
`llmgateway.client.LLMClient`.

Per docs/rules/architecture.md's Decoupling section, this is the one place
in the codebase that imports `litellm` (and, transitively, `httpx`) for LLM
calls — `llmgateway/` itself has zero such imports (see its module
docstrings). Talks to any OpenAI-compatible endpoint via LiteLLM's `openai/`
provider prefix plus a custom `api_base`; LM Studio locally
(`http://host.docker.internal:1234/v1`, per the C4 doc's Level 3 diagram) is
the configured provider for this phase, but nothing here is LM-Studio
specific — swapping to a hosted provider is a config change, not a code
change.

Every call carries an explicit `timeout` and is translated into one of
`llmgateway.exceptions`'s typed errors before it crosses back into
`llmgateway` — never a raw `litellm`/`openai`/`httpx` exception.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import litellm
import openai

from llmgateway.client import LLMClient
from llmgateway.exceptions import LLMMalformedResponseError, LLMUnreachableError
from llmgateway.models import CompletionRequest, CompletionResponse, TokenUsage

logger = logging.getLogger(__name__)

# litellm prints a noisy "Give Feedback / Get Help" banner straight to
# stdout on every failed call by default, which would otherwise pollute the
# structured JSON log stream this phase's Observability section requires
# around the LiteLLM call boundary — suppressed here, the one place
# `litellm` is imported.
litellm.suppress_debug_info = True

# litellm's own INFO log lines ("LiteLLM:INFO: utils.py ...") also go to stdout
# and would break the JSON-lines log stream, so keep only warnings and errors.
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

# litellm normalizes many distinct failure modes for a custom OpenAI-
# compatible `api_base` down to the same exception *type*
# (`litellm.exceptions.InternalServerError`) — confirmed empirically
# against a local stub server: connection-refused, invalid JSON, and a
# response missing `choices` all raised that one type with only the
# message text differing. The message text is therefore the only reliable
# signal available for telling "endpoint unreachable" (this list) apart
# from "endpoint responded, but with a malformed body" (everything else
# `_classify_api_error` doesn't match here).
_CONNECTION_FAILURE_MARKERS = (
    "connection error",
    "connection refused",
    "name or service not known",
    "nodename nor servname",
    "failed to establish a new connection",
)


class LiteLLMClient:
    """`llmgateway.client.LLMClient` implementation backed by LiteLLM.

    `endpoint` is the full OpenAI-compatible base URL (e.g.
    `http://host.docker.internal:1234/v1`); `api_key` is sent as-is (LM
    Studio ignores it, but the OpenAI-compatible wire format requires the
    header to be present, and a real hosted provider needs a real key
    here).
    """

    def __init__(self, *, endpoint: str, api_key: str) -> None:
        self._endpoint = endpoint
        self._api_key = api_key

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        started = time.monotonic()
        logger.info(
            "llm completion requested",
            extra={"llm_endpoint": self._endpoint, "llm_model": request.model, "timeout": timeout},
        )
        try:
            raw = await litellm.acompletion(
                model=f"openai/{request.model}",
                api_base=self._endpoint,
                api_key=self._api_key,
                messages=[message.model_dump() for message in request.messages],
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                timeout=timeout,
            )
        except litellm.exceptions.Timeout as exc:
            self._log_failure(started, request.model, "LLMUnreachableError")
            raise LLMUnreachableError(endpoint=self._endpoint, detail=str(exc)) from exc
        except litellm.exceptions.APIConnectionError as exc:
            self._log_failure(started, request.model, "LLMUnreachableError")
            raise LLMUnreachableError(endpoint=self._endpoint, detail=str(exc)) from exc
        except openai.APIError as exc:
            error_cls = _classify_api_error(exc)
            self._log_failure(started, request.model, error_cls.__name__)
            raise error_cls(endpoint=self._endpoint, detail=str(exc)) from exc
        except Exception as exc:  # defensive: any other transport failure not covered above
            self._log_failure(started, request.model, "LLMUnreachableError")
            raise LLMUnreachableError(endpoint=self._endpoint, detail=str(exc)) from exc

        try:
            response = _to_completion_response(raw)
        except (LookupError, AttributeError, TypeError, ValueError) as exc:
            self._log_failure(started, request.model, "LLMMalformedResponseError")
            raise LLMMalformedResponseError(endpoint=self._endpoint, detail=str(exc)) from exc

        logger.info(
            "llm completion succeeded",
            extra={
                "llm_endpoint": self._endpoint,
                "llm_model": response.model,
                "latency_seconds": round(time.monotonic() - started, 3),
            },
        )
        return response

    def _log_failure(self, started: float, model: str, error_class: str) -> None:
        logger.error(
            "llm completion failed",
            extra={
                "llm_endpoint": self._endpoint,
                "llm_model": model,
                "latency_seconds": round(time.monotonic() - started, 3),
                "error_class": error_class,
            },
        )


def _classify_api_error(
    exc: openai.APIError,
) -> type[LLMUnreachableError] | type[LLMMalformedResponseError]:
    message = str(exc).lower()
    if any(marker in message for marker in _CONNECTION_FAILURE_MARKERS):
        return LLMUnreachableError
    return LLMMalformedResponseError


def _to_completion_response(raw: Any) -> CompletionResponse:
    """Map a raw `litellm.ModelResponse` onto our stable, validated
    `CompletionResponse` shape.

    Takes `Any` rather than `litellm.ModelResponse` deliberately: the parts
    of that type this function reaches into (`.choices[0].message.content`)
    are typed loosely enough upstream (streaming vs. non-streaming choice
    unions) that a strict type here would require asserting the same facts
    this function's own `try`/`except` in `complete()` already exists to
    catch defensively — an `AttributeError`/`IndexError` here is exactly
    the "malformed response" case this adapter must turn into a typed
    `LLMMalformedResponseError`, not a `mypy --strict` puzzle to solve away.
    """
    choice = raw.choices[0]
    content = choice.message.content
    if content is None:
        raise ValueError("provider response's first choice had no message content")

    usage: TokenUsage | None = None
    if raw.usage is not None:
        usage = TokenUsage(
            prompt_tokens=raw.usage.prompt_tokens,
            completion_tokens=raw.usage.completion_tokens,
            total_tokens=raw.usage.total_tokens,
        )

    return CompletionResponse(
        content=content,
        model=raw.model,
        finish_reason=choice.finish_reason,
        usage=usage,
    )


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the domain-defined Protocol it's built to implement.
_: type[LLMClient] = LiteLLMClient
