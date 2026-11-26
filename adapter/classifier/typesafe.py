"""adapter.classifier.typesafe: the TypeSafe `Classifier` backend (ADR-0014).

`POST {endpoint}/v1/systemone` with bearer auth, over the shared egress guard
(`adapter/egress/`): the token is attached by `GuardedTransport` only, never
here. v1 sends `noul` questions only, and any other answer type fails the whole
response.

Failures are the `llmgateway.exceptions` family, each carrying a closed
`outcome` (`unauthorized`, `unreachable`, `malformed`, `rate_limited`). 429/529
are retried at most twice with the shared `retry-after` value (capped at the
lesser of 10 s and the remaining call timeout); 5xx and timeouts are not.
A `CircuitBreaker` shared by the worker stops retry amplification.

No token, request state or response body is ever in an exception or a log line.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final, Literal
from urllib.parse import urlsplit

import httpx2
from pydantic import BaseModel, Field, ValidationError

from adapter.egress.egress import endpoint_origin as egress_endpoint_origin
from adapter.egress.transport import (
    EgressOutcome,
    EgressPolicy,
    GuardedTransport,
    ProviderCredential,
    Resolver,
    build_http_client,
    system_resolver,
)
from llmgateway.classifier import (
    ClassificationRequest,
    ClassificationResponse,
    ClassifierUsage,
    NoulAnswer,
)
from llmgateway.classifier_telemetry import CallOutcome, record_call, record_token_usage
from llmgateway.exceptions import LLMMalformedResponseError, LLMUnreachableError

logger = logging.getLogger(__name__)

BACKEND: Final = "typesafe"
MAX_RETRIES: Final = 2
RETRY_CAP_SECONDS: Final = 10.0
_BACKOFF_SECONDS: Final = 0.5  # used only when the provider sends no retry-after
_RETRYABLE: Final = (429, 529)


class UnauthorizedError(LLMUnreachableError):
    outcome: CallOutcome = "unauthorized"


class RateLimitedError(LLMUnreachableError):
    outcome: CallOutcome = "rate_limited"


def outcome_of(exc: Exception) -> CallOutcome:
    """The closed outcome label for a failure this adapter raised."""
    if isinstance(exc, (UnauthorizedError, RateLimitedError)):
        return exc.outcome
    return "malformed" if isinstance(exc, LLMMalformedResponseError) else "unreachable"


class CircuitBreaker:
    """Opens after `threshold` consecutive 429/529; one probe per `cooldown`.

    Share one instance per (endpoint origin, credential_ref) via `BreakerRegistry`.
    """

    def __init__(
        self,
        *,
        threshold: int = 5,
        cooldown: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock
        self._failures = 0
        self._opened_at = 0.0

    def allow(self) -> bool:
        if self._failures < self._threshold:
            return True
        now = self._clock()
        if now - self._opened_at >= self._cooldown:
            self._opened_at = now  # half-open: let this one call probe
            return True
        return False

    def record_throttled(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._clock()

    def record_success(self) -> None:
        self._failures = 0


def _breaker_key(endpoint_url: str) -> str:
    """Scheme, canonical host and effective port, so spellings of one host share a breaker."""
    origin = egress_endpoint_origin(endpoint_url)
    if origin is None:  # unusable URL: still a stable per-spelling key
        return endpoint_url.lower()
    return f"{urlsplit(endpoint_url).scheme.lower()}://{origin[0]}:{origin[1]}"


class BreakerRegistry:
    """One breaker per (endpoint origin, credential_ref), owned by the composition root.

    A throttled endpoint or credential trips only its own key, so one org's 429s never
    open the circuit for another org's endpoint, nor for the Admin probe (which owns a
    separate registry).
    shortcut: entries are never evicted; keys are bounded by configured settings.
    """

    def __init__(self) -> None:
        self._breakers: dict[tuple[str, str], CircuitBreaker] = {}

    def for_key(self, endpoint_url: str, credential_ref: str) -> CircuitBreaker:
        key = (_breaker_key(endpoint_url), credential_ref)
        return self._breakers.setdefault(key, CircuitBreaker())


class _WireNoul(BaseModel):
    type: Literal["noul"]
    noul: float = Field(ge=0.0, le=1.0)


class _WireUsage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class _WireResponse(BaseModel):
    model: str
    answers: dict[str, _WireNoul]
    usage: _WireUsage | None = None


class TypeSafeClassifier:
    def __init__(
        self,
        endpoint_url: str,
        *,
        credential: ProviderCredential,
        policy: EgressPolicy,
        breaker: CircuitBreaker,
        resolver: Resolver = system_resolver,
        inner_transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = endpoint_url.rstrip("/")
        parts = urlsplit(endpoint_url)
        self._endpoint = f"{parts.scheme}://{parts.netloc}"  # origin only: safe to put in errors
        self._credential = credential
        self._policy = policy
        self._breaker = breaker
        self._resolver = resolver
        self._inner = inner_transport
        self._sleep = sleep

    async def classify(
        self, request: ClassificationRequest, *, timeout: float
    ) -> ClassificationResponse:
        started = time.monotonic()
        try:
            response = await self._call(request, timeout=timeout, started=started)
        except (LLMUnreachableError, LLMMalformedResponseError) as exc:
            record_call(
                backend=BACKEND,
                outcome=outcome_of(exc),
                duration_ms=(time.monotonic() - started) * 1000,
            )
            raise
        record_call(backend=BACKEND, outcome="ok", duration_ms=(time.monotonic() - started) * 1000)
        if response.usage is not None:
            record_token_usage(response.usage, backend=BACKEND)
        return response

    def _unreachable(self, detail: str) -> LLMUnreachableError:
        return LLMUnreachableError(endpoint=self._endpoint, detail=detail)

    def _body(self, request: ClassificationRequest) -> dict[str, object]:
        return {
            "model": request.model,
            "state": request.state,
            "questions": {
                qid: {"type": "noul", "instructions": q.instructions}
                for qid, q in request.questions.items()
            },
        }

    async def _call(
        self, request: ClassificationRequest, *, timeout: float, started: float
    ) -> ClassificationResponse:
        transport = GuardedTransport(
            self._url,
            policy=self._policy,
            credential=self._credential,
            resolver=self._resolver,
            inner=self._inner,
        )
        body = self._body(request)
        async with build_http_client(transport) as http:
            for attempt in range(MAX_RETRIES + 1):
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise self._unreachable("call timed out")
                if not self._breaker.allow():
                    raise RateLimitedError(endpoint=self._endpoint, detail="circuit breaker open")
                transport.status = transport.retry_after_seconds = None
                try:
                    resp = await asyncio.wait_for(
                        http.post(f"{self._url}/v1/systemone", json=body), remaining
                    )
                except (httpx2.HTTPError, TimeoutError) as exc:
                    # Class name only: str(exc) is not guaranteed token-free.
                    if transport.outcome in (EgressOutcome.MALFORMED, EgressOutcome.OVERSIZED):
                        raise LLMMalformedResponseError(
                            endpoint=self._endpoint, detail="response refused by the egress guard"
                        ) from None
                    raise self._unreachable(f"request failed ({type(exc).__name__})") from None
                if resp.status_code not in _RETRYABLE:
                    # Any non-throttle reply (even a 5xx) proves the endpoint is not
                    # throttling, so it closes a half-open breaker.
                    self._breaker.record_success()
                    return self._finish(resp, set(request.questions))
                self._breaker.record_throttled()
                if attempt == MAX_RETRIES:
                    break
                remaining = timeout - (time.monotonic() - started)
                wait = float(
                    transport.retry_after_seconds
                    if transport.retry_after_seconds is not None
                    else _BACKOFF_SECONDS * 2**attempt
                )
                if wait >= remaining:
                    break  # no time left to retry after waiting
                await self._sleep(min(wait, RETRY_CAP_SECONDS))
        raise RateLimitedError(endpoint=self._endpoint, detail="rate limited after retries")

    def _finish(self, resp: httpx2.Response, ids: set[str]) -> ClassificationResponse:
        status = resp.status_code
        if status in (401, 403):
            logger.error("classifier provider rejected the credential (HTTP %d)", status)
            raise UnauthorizedError(endpoint=self._endpoint, detail="credential rejected")
        if status >= 500:
            raise self._unreachable(f"provider error (HTTP {status})")
        if status != 200:
            raise LLMMalformedResponseError(
                endpoint=self._endpoint, detail=f"request rejected (HTTP {status})"
            )
        return self._parse(resp, ids)

    def _parse(self, resp: httpx2.Response, ids: set[str]) -> ClassificationResponse:
        try:
            wire = _WireResponse.model_validate_json(resp.content)
        except ValidationError:  # not str(exc): pydantic echoes input values
            raise LLMMalformedResponseError(
                endpoint=self._endpoint, detail="response does not match the noul answer shape"
            ) from None
        if set(wire.answers) != ids:
            raise LLMMalformedResponseError(
                endpoint=self._endpoint, detail="answers do not match the question ids"
            )
        return ClassificationResponse(
            model=wire.model,
            answers={k: NoulAnswer(p=v.noul) for k, v in wire.answers.items()},
            usage=ClassifierUsage(**wire.usage.model_dump()) if wire.usage else None,
        )
