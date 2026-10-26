"""
adapter.product_knowledge.client: the one generic MCP client for the product-knowledge contract.

This is the optional product-knowledge contract's client (ADR-0013 decision 1, plan Phase 4).

**The only module outside ``conformance.py`` (the in-package self-test)
that imports ``mcp``.** It implements the consumer-defined
``review.product_knowledge.ProductKnowledgeProvider`` Protocol with the
official SDK's ``Client`` in ``mode="auto"`` (modern ``server/discover``
first, legacy ``initialize`` fallback) over an ``httpx2`` client whose only
transport is ``transport.GuardedTransport`` — so every byte the SDK sends is
subject to the egress guard, the response cap and the per-request token
attachment.

Behavior pinned here (each has a test in ``test_client.py``):

* **Stateless operations.** ``negotiate``/``search``/``fetch`` each open one
  short session (connect 2 s, call 5 s via ``asyncio.timeout`` — httpx read
  timeouts alone are evaded by a slow drip), so no connection state outlives
  a job and nothing is shared across orgs.
* **Negotiation** is the dedicated ``product_knowledge_contract`` tool (never
  ``serverInfo``): highest major both sides support; missing/malformed ⇒ no
  overlap ⇒ ``unsupported_version``. Cached (10 min, bounded, single-flight,
  keyed by endpoint + credential *ref*, never the token) and invalidated by a
  provider ``unsupported_version`` error.
* **Only declared arguments are sent**: a request argument is included only
  if the tool's own ``inputSchema`` declares it.
* **Provider text is never read for behavior or echoed.** Tool descriptions,
  annotations, ``serverInfo`` and error bodies are ignored; a provider error
  contributes only its closed ``code``. Failures surface as a
  ``ProductKnowledgeError`` carrying just a ``ProviderOutcome``.
* **Fan-out is bounded**: a per-endpoint semaphore and, after a
  ``rate_limited`` answer, a short in-process cool-down (A3).
* No retry and no circuit breaker (spec; recorded triggers).

Tokens are never logged, never in an exception message, never returned.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final, TypedDict, TypeVar, Unpack
from urllib.parse import urlsplit

import httpx2
import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from adapter.product_knowledge.egress import canonicalize_host
from adapter.product_knowledge.transport import (
    EgressPolicy,
    GuardedTransport,
    ProviderCredential,
    Resolver,
    build_http_client,
    system_resolver,
)
from review.product_knowledge import (
    FETCH_CONTENT_MAX_CHARS,
    SUPPORTED_MAJORS,
    TOOL_CONTRACT,
    TOOL_FETCH,
    TOOL_RELATED,
    TOOL_SEARCH,
    TOP_K,
    ContractDeclaration,
    NegotiationResult,
    ProductKnowledgeError,
    ProviderError,
    ProviderOutcome,
)

__all__ = [
    "ClientTimeouts",
    "ClientTuning",
    "EndpointGate",
    "FactoryTuning",
    "McpProductKnowledgeClient",
    "NegotiationCache",
    "exception_leaves",
    "is_malformed",
    "network_outcome",
]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_KNOWN_TOOLS: Final = frozenset({TOOL_CONTRACT, TOOL_SEARCH, TOOL_FETCH, TOOL_RELATED})

CacheKey = tuple[str, str | None]


@dataclass(frozen=True)
class ClientTimeouts:
    connect_seconds: float = 2.0
    call_seconds: float = 5.0
    # Extra wall-clock allowance for SDK/httpx2 teardown (legacy era sends a
    # ``DELETE``), on top of connect + call: the whole session is bounded.
    teardown_grace_seconds: float = 1.0

    @property
    def session_seconds(self) -> float:
        return self.connect_seconds + self.call_seconds + self.teardown_grace_seconds


class NegotiationCache:
    """
    Bounded TTL map of negotiation results with single-flight.

    Concurrent cold callers for one key trigger exactly one negotiation. Owned by
    the composition root and passed in — not module state.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 600.0,
        max_entries: int = 64,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the cache bounds (TTL, entry count) and the clock."""
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._entries: OrderedDict[CacheKey, tuple[float, NegotiationResult]] = OrderedDict()
        # In-flight only: a lock exists while some caller holds or awaits it
        # and is dropped by the last of them (never while held or awaited).
        self._locks: dict[CacheKey, asyncio.Lock] = {}
        self._lock_users: dict[CacheKey, int] = {}
        self._last_chosen: OrderedDict[CacheKey, str | None] = OrderedDict()

    def invalidate(self, key: CacheKey) -> None:
        self._entries.pop(key, None)

    def changed(self, key: CacheKey, chosen: str | None) -> bool:
        """True the first time, and whenever ``chosen`` differs from last time."""
        previous = self._last_chosen.get(key, "<unset>")
        self._last_chosen[key] = chosen
        self._last_chosen.move_to_end(key)
        while len(self._last_chosen) > self._max:
            self._last_chosen.popitem(last=False)
        return previous != chosen

    def _fresh(self, key: CacheKey) -> NegotiationResult | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires, result = entry
        if self._clock() >= expires:
            self._entries.pop(key, None)
            return None
        return result

    async def get_or_negotiate(
        self, key: CacheKey, negotiate: Callable[[], Awaitable[NegotiationResult]]
    ) -> NegotiationResult:
        cached = self._fresh(key)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._lock_users[key] = self._lock_users.get(key, 0) + 1
        try:
            async with lock:
                cached = self._fresh(key)
                if cached is not None:
                    return cached
                result = await negotiate()
                self._entries[key] = (self._clock() + self._ttl, result)
                self._entries.move_to_end(key)
                while len(self._entries) > self._max:
                    evicted, _ = self._entries.popitem(last=False)
                    self._last_chosen.pop(evicted, None)
                return result
        finally:
            users = self._lock_users[key] - 1
            if users:
                self._lock_users[key] = users
            else:  # nobody holds or awaits it any more
                del self._lock_users[key]
                self._locks.pop(key, None)


class EndpointGate:
    """
    Process-level per-endpoint concurrency bound plus the ``rate_limited`` cool-down (A3).

    Bounded in the number of endpoints it remembers.

    The concurrency bound is keyed by *origin* (``scheme://host:port``, no
    path): it protects the server, which is the origin, so a path variation
    cannot multiply the fan-out. The cool-down is keyed by
    ``(origin, credential_ref)``: a ``rate_limited`` answer to one tenant's
    credential must not starve another credential's calls to the same
    origin (the limit is usually per token), while a path variation cannot
    dodge it.
    """

    def __init__(
        self,
        *,
        max_in_flight: int = 4,
        max_endpoints: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the per-origin concurrency bound, endpoint bound and clock."""
        self._max_in_flight = max_in_flight
        self._max_endpoints = max_endpoints
        self._clock = clock
        self._semaphores: OrderedDict[str, asyncio.Semaphore] = OrderedDict()
        self._cooldowns: dict[tuple[str, str | None], float] = {}
        # Holders + waiters per endpoint, tracked explicitly (never read off
        # the semaphore's private counter).
        self._users: dict[str, int] = {}

    def start_cooldown(self, endpoint: str, seconds: float, ref: str | None = None) -> None:
        now = self._clock()
        self._cooldowns[(endpoint, ref)] = now + seconds
        if len(self._cooldowns) > self._max_endpoints:
            # Drop expired entries first, then the soonest-to-expire ones.
            live = {k: v for k, v in self._cooldowns.items() if v > now}
            while len(live) > self._max_endpoints:
                del live[min(live, key=lambda k: live[k])]
            self._cooldowns = live

    def cooling(self, endpoint: str, ref: str | None = None) -> bool:
        until = self._cooldowns.get((endpoint, ref))
        if until is None:
            return False
        if self._clock() >= until:
            del self._cooldowns[(endpoint, ref)]
            return False
        return True

    def _semaphore(self, endpoint: str) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(endpoint)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._max_in_flight)
            self._semaphores[endpoint] = semaphore
            while len(self._semaphores) > self._max_endpoints:
                # Evict the oldest *idle* endpoint only; a held (or awaited:
                # waiters imply a held slot) semaphore stays.
                for old in self._semaphores:
                    if old != endpoint and not self._users.get(old):
                        del self._semaphores[old]
                        break
                else:
                    break
        return semaphore

    @asynccontextmanager
    async def slot(self, endpoint: str, ref: str | None = None) -> AsyncIterator[None]:
        if self.cooling(endpoint, ref):
            raise ProductKnowledgeError(ProviderOutcome.PROVIDER_ERROR)
        semaphore = self._semaphore(endpoint)
        self._users[endpoint] = self._users.get(endpoint, 0) + 1
        try:
            async with semaphore:
                yield
        finally:
            left = self._users[endpoint] - 1
            if left:
                self._users[endpoint] = left
            else:
                del self._users[endpoint]


def _silence_sdk_logging() -> None:
    """
    Silence the SDK's own logger namespace.

    The SDK logs response-parsing failures with ``logger.exception``,
    whose traceback carries the provider's raw bytes (pydantic ``input_value``).
    Provider content must never reach logs (US-PK6/PK14), so the SDK's own
    logger namespace is silenced; this adapter logs outcomes itself.
    Idempotent. ponytail: process-global; a per-record scrubber if operators
    ever need SDK debug output.
    """
    logging.getLogger("mcp").setLevel(logging.CRITICAL)


def exception_leaves(exc: BaseException) -> Iterator[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from exception_leaves(inner)
    else:
        yield exc
        if exc.__cause__ is not None and exc.__cause__ is not exc:
            yield from exception_leaves(exc.__cause__)


def _typed_leaf_outcome(leaves: list[BaseException]) -> ProviderOutcome | None:
    """
    Return the specific outcome of our own typed error, if one was raised.

    Our own typed errors raised inside the SDK's task group arrive wrapped
    in an ExceptionGroup; keep their specific outcome.
    """
    for leaf in leaves:
        if isinstance(leaf, ProductKnowledgeError):
            return leaf.outcome
    return None


def network_outcome(leaves: list[BaseException]) -> ProviderOutcome | None:
    if any(isinstance(e, TimeoutError | httpx2.TimeoutException) for e in leaves):
        return ProviderOutcome.TIMEOUT
    if any(isinstance(e, httpx2.TransportError) for e in leaves):
        return ProviderOutcome.UNREACHABLE
    return None


def is_malformed(leaves: list[BaseException]) -> bool:
    return any(
        isinstance(e, RecursionError | json.JSONDecodeError | ValidationError)
        or (isinstance(e, MCPError) and e.code == types.PARSE_ERROR)
        for e in leaves
    )


class FactoryTuning(TypedDict, total=False):
    """
    Keyword options shared by the client and its factory.

    Grouped as an ``Unpack`` TypedDict (not a config object) so every call site
    keeps its keyword spelling while ``__init__`` stays under the parameter limit.
    """

    cache: NegotiationCache | None
    gate: EndpointGate | None
    bypass_cache: bool
    top_k: int


class ClientTuning(FactoryTuning, total=False):
    supported_majors: frozenset[str]
    deprecated_majors: frozenset[str]
    rate_limit_cooldown_seconds: float


def _reject_unknown_tuning(tuning: Mapping[str, object]) -> None:
    """
    Raise ``TypeError`` for an option ``**tuning`` should not have accepted.

    ``**tuning`` is not checked by the interpreter the way named keyword
    parameters are: an unknown option must still be a ``TypeError``.
    """
    unknown = sorted(set(tuning) - ClientTuning.__optional_keys__)
    if unknown:
        raise TypeError(f"unexpected keyword argument {unknown[0]!r}")


def _endpoint_identity(
    endpoint_url: str, credential_ref: str | None
) -> tuple[bool, str, str, CacheKey]:
    """
    Return ``(usable, host, origin, cache_key)`` for an endpoint URL.

    Total: a malformed URL ('host:abc', port > 65535, an unbalanced IPv6
    bracket) is unusable and every call then fails closed with
    ``blocked_egress`` instead of raising.
    """
    try:
        parts = urlsplit(endpoint_url)
        port = parts.port
        host = canonicalize_host(parts.hostname or "") or "invalid"
        scheme = parts.scheme.lower()
        origin = f"{scheme}://{host}:{port or (443 if scheme == 'https' else 80)}"
        return True, host, origin, (f"{scheme}://{host}:{port or ''}{parts.path}", credential_ref)
    except ValueError:
        return False, "invalid", "invalid://invalid", ("invalid://invalid", credential_ref)


class McpProductKnowledgeClient:
    """
    Implements ``ProductKnowledgeProvider`` for one endpoint (+credential).

    Constructed from already-parsed config only — no env or file reads.
    ``resolver`` and ``inner_transport`` are test seams (a fake resolver and a
    transport that connects to a loopback fake); production passes neither.
    The remaining options are ``ClientTuning`` keywords (``cache``, ``gate``,
    ``supported_majors``, ``deprecated_majors``,
    ``rate_limit_cooldown_seconds``, ``bypass_cache``, ``top_k``).
    """

    def __init__(
        self,
        endpoint_url: str,
        *,
        credential: ProviderCredential | None = None,
        policy: EgressPolicy | None = None,
        timeouts: ClientTimeouts | None = None,
        resolver: Resolver = system_resolver,
        inner_transport: httpx2.AsyncBaseTransport | None = None,
        **tuning: Unpack[ClientTuning],
    ) -> None:
        """Bind one endpoint (+credential) from already-parsed config."""
        _reject_unknown_tuning(tuning)
        _silence_sdk_logging()
        self._url = endpoint_url
        self._credential = credential
        self._policy = policy or EgressPolicy()
        self._timeouts = timeouts or ClientTimeouts()
        self._cache = tuning.get("cache") or NegotiationCache()
        self._gate = tuning.get("gate") or EndpointGate()
        self._supported = tuning.get("supported_majors", SUPPORTED_MAJORS)
        self._deprecated = tuning.get("deprecated_majors", frozenset())
        self._cooldown = tuning.get("rate_limit_cooldown_seconds", 30.0)
        self._bypass_cache = tuning.get("bypass_cache", False)
        # Latest negotiation of a bypass_cache client (test-connection): the
        # route negotiates once, search()/fetch() reuse that result.
        self._probe_negotiation: NegotiationResult | None = None
        self._top_k = tuning.get("top_k", TOP_K)
        self._resolver = resolver
        self._inner = inner_transport
        self._usable, self._host, self._origin, self._cache_key = _endpoint_identity(
            endpoint_url, credential.ref if credential else None
        )

    # --- ProductKnowledgeProvider ------------------------------------------

    async def negotiate(self) -> NegotiationResult:
        if self._bypass_cache:
            self._probe_negotiation = await self._negotiate_uncached()
            return self._probe_negotiation
        return await self._cache.get_or_negotiate(self._cache_key, self._negotiate_uncached)

    async def search(self, query: str) -> object:
        version = await self._chosen_version()
        return await self._call(
            "search",
            TOOL_SEARCH,
            {"contract_version": version, "query": query, "top_k": self._top_k},
        )

    async def fetch(self, item_id: str) -> object:
        version = await self._chosen_version()
        return await self._call(
            "fetch",
            TOOL_FETCH,
            {
                "contract_version": version,
                "id": item_id,
                "max_chars": FETCH_CONTENT_MAX_CHARS,
            },
        )

    # --- negotiation ----------------------------------------------------------

    async def _chosen_version(self) -> str:
        # A bypass_cache client (test-connection) already negotiated for the
        # route; search/fetch reuse that result instead of negotiating again.
        result = self._probe_negotiation or await self.negotiate()
        if result.chosen_version is None:
            raise ProductKnowledgeError(ProviderOutcome.UNSUPPORTED_VERSION)
        return result.chosen_version

    async def _negotiate_uncached(self) -> NegotiationResult:
        async def body(sdk: Client) -> NegotiationResult:
            names = [t.name for t in (await sdk.list_tools()).tools]
            present = [n for n in names if n in _KNOWN_TOOLS]
            if TOOL_CONTRACT not in present:
                return NegotiationResult(
                    chosen_version=None, supported_versions=[], tools_present=present
                )
            raw = self._extract(
                await sdk.session.call_tool(TOOL_CONTRACT, {}, allow_input_required=True)
            )
            try:
                declared = ContractDeclaration.model_validate(raw).supported_versions
            except ValidationError:
                return NegotiationResult(
                    chosen_version=None, supported_versions=[], tools_present=present
                )
            common = [v for v in declared if v in self._supported]
            chosen = max(common, key=int) if common else None
            return NegotiationResult(
                chosen_version=chosen,
                supported_versions=declared,
                tools_present=present,
                deprecated=chosen is not None and chosen in self._deprecated,
            )

        result: NegotiationResult = await self._run("contract", body)
        if self._cache.changed(self._cache_key, result.chosen_version):
            logger.info(
                "product-knowledge contract negotiation result",
                extra={
                    "endpoint_host": self._host,
                    "contract_version": result.chosen_version or "none",
                    "deprecated_contract_version": result.deprecated,
                },
            )
        return result

    # --- one tool call ---------------------------------------------------------

    async def _call(self, operation: str, tool: str, wanted: dict[str, Any]) -> object:
        async def body(sdk: Client) -> object:
            tools = (await sdk.list_tools()).tools
            spec = next((t for t in tools if t.name == tool), None)
            if spec is None:
                raise ProductKnowledgeError(ProviderOutcome.UNSUPPORTED_VERSION)
            properties = spec.input_schema.get("properties")
            declared = set(properties) if isinstance(properties, dict) else set()
            arguments = {k: v for k, v in wanted.items() if k in declared}
            return self._extract(
                await sdk.session.call_tool(tool, arguments, allow_input_required=True)
            )

        return await self._run(operation, body)

    def _extract(self, result: types.CallToolResult | types.InputRequiredResult) -> object:
        """Raw ``structuredContent`` of a successful result, else a typed error."""
        if isinstance(result, types.InputRequiredResult):
            raise ProductKnowledgeError(ProviderOutcome.PROVIDER_ERROR)
        if result.is_error:
            raise ProductKnowledgeError(self._error_outcome(result.structured_content))
        if result.structured_content is None:
            raise ProductKnowledgeError(ProviderOutcome.MALFORMED)
        return result.structured_content

    def _error_outcome(self, structured: object) -> ProviderOutcome:
        try:
            code = ProviderError.model_validate(structured).error.code
        except ValidationError:
            return ProviderOutcome.PROVIDER_ERROR
        if code in ("unauthorized", "forbidden"):
            return ProviderOutcome.AUTH_FAILED
        if code == "unsupported_version":
            self._cache.invalidate(self._cache_key)
            self._probe_negotiation = None
            return ProviderOutcome.UNSUPPORTED_VERSION
        if code == "rate_limited":
            self._gate.start_cooldown(self._origin, self._cooldown, self._cache_key[1])
        return ProviderOutcome.PROVIDER_ERROR

    # --- session plumbing --------------------------------------------------------

    async def _run(self, operation: str, body: Callable[[Client], Awaitable[_T]]) -> _T:
        if not self._usable:
            raise ProductKnowledgeError(ProviderOutcome.BLOCKED_EGRESS)
        endpoint = self._origin
        transport = GuardedTransport(
            self._url,
            policy=self._policy,
            credential=self._credential,
            resolver=self._resolver,
            inner=self._inner,
        )
        started = time.monotonic()
        outcome = ProviderOutcome.OK
        try:
            async with self._gate.slot(endpoint, self._cache_key[1]):
                failure: ProviderOutcome
                try:
                    return await self._session(transport, body)
                except ProductKnowledgeError:
                    raise
                except Exception as exc:
                    failure = self._classify(exc, transport, endpoint)
                # Raised outside the ``except`` block on purpose: no
                # ``__context__`` chain, so provider bytes inside the SDK's
                # pydantic errors can never ride a logged traceback.
                raise ProductKnowledgeError(failure, status_code=transport.status)
        except ProductKnowledgeError as err:
            outcome = err.outcome
            if err.status_code is None:
                err.status_code = transport.status
            raise
        finally:
            logger.log(
                logging.DEBUG if outcome is ProviderOutcome.OK else logging.INFO,
                "product-knowledge call",
                extra={
                    "endpoint_host": self._host,
                    "operation": operation,
                    "outcome": outcome.value,
                    "status_code": transport.status,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )

    async def _session(
        self, transport: GuardedTransport, body: Callable[[Client], Awaitable[_T]]
    ) -> _T:
        # One hard wall-clock deadline around the whole session *including*
        # the exit-stack unwind: a provider that drips or hangs the legacy
        # ``DELETE`` (httpx2 read timeouts are per-read) must not outlive it
        # or hold the per-endpoint slot. The inner deadlines keep connect and
        # call individually attributable.
        async with asyncio.timeout(self._timeouts.session_seconds), AsyncExitStack() as stack:
            http = await stack.enter_async_context(build_http_client(transport))
            sdk = Client(
                streamable_http_client(self._url, http_client=http),
                mode="auto",
                cache=None,
            )
            async with asyncio.timeout(self._timeouts.connect_seconds):
                await stack.enter_async_context(sdk)
            logger.debug(
                "product-knowledge protocol era",
                extra={"endpoint_host": self._host, "protocol_version": sdk.protocol_version},
            )
            async with asyncio.timeout(self._timeouts.call_seconds):
                return await body(sdk)

    def _status_outcome(self, transport: GuardedTransport, endpoint: str) -> ProviderOutcome | None:
        """
        Return the outcome implied by the last HTTP error status, if any.

        A 429 also starts the endpoint cool-down.
        """
        status = transport.status
        if status in (401, 403):
            return ProviderOutcome.AUTH_FAILED
        if status == 429:
            retry = transport.retry_after_seconds
            self._gate.start_cooldown(
                endpoint,
                min(float(retry), 300.0) if retry is not None else self._cooldown,
                self._cache_key[1],
            )
            return ProviderOutcome.PROVIDER_ERROR
        return None

    def _classify(
        self, exc: BaseException, transport: GuardedTransport, endpoint: str
    ) -> ProviderOutcome:
        if transport.outcome is not None:
            return transport.outcome
        leaves = list(exception_leaves(exc))
        # Order is the precedence: typed errors, then timeout, then transport
        # failure, then the HTTP status, then malformed bodies.
        found = _typed_leaf_outcome(leaves)
        if found is None:
            found = network_outcome(leaves)
        if found is None:
            found = self._status_outcome(transport, endpoint)
        if found is not None:
            return found
        return ProviderOutcome.MALFORMED if is_malformed(leaves) else ProviderOutcome.PROVIDER_ERROR
