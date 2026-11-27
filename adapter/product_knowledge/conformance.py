"""adapter.product_knowledge.conformance: the runnable product-knowledge contract check.

It answers "does my server implement the product-knowledge contract?" (US-PK17).

Point it at any MCP endpoint::

    PK_TOKEN=... python -m adapter.product_knowledge.conformance --pk-url https://kb.example/mcp

or run it through pytest (``test_conformance.py``) with ``--pk-url``. It is
developed against the in-repo fake provider and proven able to *fail* by one
seeded-faulty fake per check (``test_conformance.py``).

Deliberately **not** the production client path: it is a developer/operator
tool talking to a URL the person running it chose, so it uses a plain
``httpx2`` client (redirects off, ``trust_env=False``) — no egress guard — and
reads raw tool results, because several checks need the exact error shape that
the production client collapses into a ``ProviderOutcome``. The strict checks
(snippet cap, ``title`` required, ``top_k``) are intentionally stricter than the
tolerant consumer.

Output never echoes provider content or the token: a failure detail is a
reviewer-written sentence plus field *paths*.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from typing import Any, Final

import httpx2
import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, SecretStr, ValidationError

from review.product_knowledge import (
    FETCH_CONTENT_MAX_CHARS,
    MAX_RESPONSE_BYTES,
    SNIPPET_MAX_CHARS,
    TITLE_MAX_CHARS,
    TOOL_CONTRACT,
    TOOL_FETCH,
    TOOL_RELATED,
    TOOL_SEARCH,
    TOP_K,
    URL_MAX_CHARS,
    ContractDeclaration,
    FetchResponse,
    ProviderError,
    SearchResponse,
)

__all__ = ["CheckResult", "ConformanceTarget", "Status", "main", "run_conformance"]

DEFAULT_QUERY: Final = "product knowledge"
_UNKNOWN_ID: Final = "conformance-unknown-id-0000"
_BAD_VERSION: Final = "99"
_INVALID_TOKEN: Final = "conformance-invalid-token"


class Status(StrEnum):
    PASS = "pass"  # nosec B105 - status label, not a password
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str = ""


@dataclass(frozen=True)
class ConformanceTarget:
    url: str
    token: SecretStr | None = None
    # A query the endpoint answers with at least one result.
    query: str = DEFAULT_QUERY
    timeout_seconds: float = 30.0


class _Failed(Exception):
    """A check failed; the message is the (provider-content-free) detail."""


class _Skipped(Exception):
    """A check does not apply; the message says why."""


# --- transport: count wire bytes per response (response_size_within_caps) ---


class _CountingStream(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream, owner: _CountingTransport) -> None:
        self._inner = inner
        self._owner = owner

    async def __aiter__(self) -> AsyncIterator[bytes]:
        total = 0
        async for chunk in self._inner:
            total += len(chunk)
            self._owner.largest = max(self._owner.largest, total)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class _CountingTransport(httpx2.AsyncBaseTransport):
    def __init__(self) -> None:
        self._inner = httpx2.AsyncHTTPTransport(trust_env=False)
        self.largest = 0  # most bytes seen in any single response body

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._inner.handle_async_request(request)
        stream = response.stream
        if not isinstance(stream, httpx2.AsyncByteStream):
            return response
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_CountingStream(stream, self),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


@asynccontextmanager
async def _open(
    url: str, token: str | None, timeout_seconds: float
) -> AsyncIterator[tuple[Client, _CountingTransport]]:
    counting = _CountingTransport()
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with httpx2.AsyncClient(
        transport=counting,
        headers=headers,
        follow_redirects=False,
        trust_env=False,
        timeout=httpx2.Timeout(timeout_seconds),
    ) as http:
        sdk = Client(streamable_http_client(url, http_client=http), mode="auto", cache=None)
        async with sdk:
            yield sdk, counting


# --- one live session --------------------------------------------------------


def _paths(error: ValidationError) -> str:
    seen = sorted({".".join(str(p) for p in e["loc"]) or "<root>" for e in error.errors()})
    return ", ".join(seen[:5])


def _shape[M: BaseModel](model: type[M], raw: object, what: str) -> M:
    try:
        return model.model_validate(raw)
    except ValidationError as err:
        raise _Failed(f"{what} does not match the contract schema (fields: {_paths(err)})") from err


class _Run:
    def __init__(self, sdk: Client, counting: _CountingTransport, target: ConformanceTarget):
        self.sdk = sdk
        self.counting = counting
        self.target = target
        self.tools: set[str] = set()
        self._search: object | None = None

    async def call(self, tool: str, arguments: dict[str, Any]) -> tuple[bool, object]:
        """``(is_error, structuredContent)`` of one tool call."""
        result = await self.sdk.session.call_tool(tool, arguments, allow_input_required=True)
        if isinstance(result, types.InputRequiredResult):
            raise _Failed(f"{tool} answered with an input-required result")
        return result.is_error, result.structured_content

    async def ok(self, tool: str, arguments: dict[str, Any]) -> object:
        is_error, structured = await self.call(tool, arguments)
        if is_error:
            raise _Failed(f"{tool} returned an error result")
        if structured is None:
            raise _Failed(f"{tool} returned no structuredContent")
        return structured

    async def error_code(self, tool: str, arguments: dict[str, Any]) -> str:
        is_error, structured = await self.call(tool, arguments)
        if not is_error:
            raise _Failed(f"{tool} did not return an error result")
        return _shape(ProviderError, structured, f"{tool} error").error.code

    async def search(self) -> object:
        if self._search is None:
            self._search = await self.ok(
                TOOL_SEARCH,
                {"contract_version": "1", "query": self.target.query, "top_k": TOP_K},
            )
        return self._search

    async def result_items(self) -> list[dict[str, Any]]:
        raw = await self.search()
        results = raw.get("results") if isinstance(raw, dict) else None
        if not isinstance(results, list) or not all(isinstance(i, dict) for i in results):
            raise _Failed("search results is not a list of objects")
        return results

    async def first_id(self) -> str:
        items = await self.result_items()
        if not items or not isinstance(items[0].get("id"), str):
            raise _Failed(
                "search returned no result for the conformance query "
                "(pass --pk-query with a term the endpoint knows)"
            )
        return str(items[0]["id"])


# --- the checks --------------------------------------------------------------


async def _contract_tool_present(run: _Run) -> str:
    run.tools = {t.name for t in (await run.sdk.list_tools()).tools}
    missing = [t for t in (TOOL_CONTRACT, TOOL_SEARCH, TOOL_FETCH) if t not in run.tools]
    if missing:
        raise _Failed("tools/list is missing: " + ", ".join(missing))
    return "contract, search and fetch tools are listed"


async def _declaration(run: _Run) -> ContractDeclaration:
    raw = await run.ok(TOOL_CONTRACT, {})
    return _shape(ContractDeclaration, raw, "product_knowledge_contract output")


async def _contract_declaration_valid(run: _Run) -> str:
    declared = await _declaration(run)
    return f"declares majors {declared.supported_versions}"


async def _version_1_declared(run: _Run) -> str:
    if "1" not in (await _declaration(run)).supported_versions:
        raise _Failed('supported_versions does not include "1"')
    return 'major "1" declared'


async def _search_schema(run: _Run) -> str:
    _shape(SearchResponse, await run.search(), "search output")
    return "search output matches the schema"


async def _search_titles(run: _Run) -> str:
    items = await run.result_items()
    for index, item in enumerate(items):
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise _Failed(f"results[{index}] has no non-empty title (required since contract v1)")
    return f"all {len(items)} results carry a title"


async def _search_ordering(run: _Run) -> str:
    scores = [
        float(s)
        for s in (i.get("score") for i in await run.result_items())
        if isinstance(s, int | float) and not isinstance(s, bool)
    ]
    if len(scores) < 2:
        raise _Skipped("no scores to order (fewer than two scored results)")
    if any(a < b for a, b in pairwise(scores)):
        raise _Failed("results are not ordered best-first (scores increase down the list)")
    return "results are ordered best-first"


async def _search_limits(run: _Run) -> str:
    items = await run.result_items()
    if len(items) > TOP_K:
        raise _Failed(f"search returned {len(items)} results for top_k={TOP_K}")
    caps = (("snippet", SNIPPET_MAX_CHARS), ("title", TITLE_MAX_CHARS), ("url", URL_MAX_CHARS))
    for index, item in enumerate(items):
        for field, cap in caps:
            value = item.get(field)
            if isinstance(value, str) and len(value) > cap:
                raise _Failed(f"results[{index}].{field} exceeds {cap} characters")
    return "result count and field lengths are within the limits"


async def _top_k_honored(run: _Run) -> str:
    raw = await run.ok(
        TOOL_SEARCH, {"contract_version": "1", "query": run.target.query, "top_k": 1}
    )
    results = raw.get("results") if isinstance(raw, dict) else None
    if not isinstance(results, list):
        raise _Failed("search output has no results list")
    if len(results) > 1:
        raise _Failed(f"search returned {len(results)} results for top_k=1")
    return "top_k=1 returns at most one result"


async def _fetch_returned_id(run: _Run) -> str:
    wanted = await run.first_id()
    raw = await run.ok(
        TOOL_FETCH,
        {"contract_version": "1", "id": wanted, "max_chars": FETCH_CONTENT_MAX_CHARS},
    )
    fetched = _shape(FetchResponse, raw, "fetch output")
    if fetched.id != wanted:
        raise _Failed("fetch returned a different id than the one requested")
    if len(fetched.content) > FETCH_CONTENT_MAX_CHARS:
        raise _Failed(f"fetch content exceeds the requested max_chars ({FETCH_CONTENT_MAX_CHARS})")
    return "fetch of a returned id conforms"


async def _fetch_unknown_id_not_found(run: _Run) -> str:
    code = await run.error_code(
        TOOL_FETCH, {"contract_version": "1", "id": _UNKNOWN_ID, "max_chars": 256}
    )
    if code != "not_found":
        raise _Failed(f'fetch of an unknown id returned error code "{code}", not "not_found"')
    return "unknown id yields the not_found error shape"


async def _unsupported_version_rejected(run: _Run) -> str:
    code = await run.error_code(
        TOOL_SEARCH, {"contract_version": _BAD_VERSION, "query": run.target.query, "top_k": 1}
    )
    if code != "unsupported_version":
        raise _Failed(f'an unsupported contract_version returned "{code}", not unsupported_version')
    return "unsupported contract_version yields unsupported_version"


async def _related_shape(run: _Run) -> str:
    if TOOL_RELATED not in run.tools:
        raise _Skipped("optional related tool is not offered")
    raw = await run.ok(
        TOOL_RELATED, {"contract_version": "1", "id": await run.first_id(), "top_k": 1}
    )
    _shape(SearchResponse, raw, "related output")
    return "related output matches the search result shape"


async def _response_size_within_caps(run: _Run) -> str:
    if run.counting.largest > MAX_RESPONSE_BYTES:
        raise _Failed(
            f"a response body was {run.counting.largest} bytes; the cap is {MAX_RESPONSE_BYTES}"
        )
    return f"largest response was {run.counting.largest} bytes"


# Order matters only for the size check, which must see every other response.
_CHECKS: Final[tuple[tuple[str, Callable[[_Run], Awaitable[str]]], ...]] = (
    ("contract_tool_present", _contract_tool_present),
    ("contract_declaration_valid", _contract_declaration_valid),
    ("version_1_declared", _version_1_declared),
    ("search_schema", _search_schema),
    ("search_titles", _search_titles),
    ("search_ordering", _search_ordering),
    ("search_limits", _search_limits),
    ("top_k_honored", _top_k_honored),
    ("fetch_returned_id", _fetch_returned_id),
    ("fetch_unknown_id_not_found", _fetch_unknown_id_not_found),
    ("unsupported_version_rejected", _unsupported_version_rejected),
    ("related_shape", _related_shape),
    ("response_size_within_caps", _response_size_within_caps),
)


# --- auth (separate sessions) -------------------------------------------------


def _describe(exc: BaseException) -> str:
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__} (HTTP {status})" if status else type(exc).__name__


async def _accepted(target: ConformanceTarget, token: str | None) -> bool:
    """Does the server answer ``tools/list`` for this credential?"""
    try:
        async with asyncio.timeout(target.timeout_seconds):
            async with _open(target.url, token, target.timeout_seconds) as (sdk, _):
                await sdk.list_tools()
    except TimeoutError as err:
        raise _Failed("the unauthenticated probe timed out instead of being rejected") from err
    except Exception:
        return False
    return True


async def _auth_rejects_bad_credentials(target: ConformanceTarget) -> CheckResult:
    name = "auth_rejects_missing_and_invalid_token"
    if target.token is None:
        return CheckResult(name, Status.SKIP, "no token supplied (set PK_TOKEN) so no auth claimed")
    try:
        if await _accepted(target, None):
            raise _Failed("a request with no token was accepted although a token is configured")
        if await _accepted(target, _INVALID_TOKEN):
            raise _Failed("a request with an invalid token was accepted")
    except _Failed as err:
        return CheckResult(name, Status.FAIL, str(err))
    return CheckResult(name, Status.PASS, "missing and invalid tokens are rejected")


# --- runner --------------------------------------------------------------------


async def _run_checks(run: _Run) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name, check in _CHECKS:
        try:
            results.append(CheckResult(name, Status.PASS, await check(run)))
        except _Skipped as skip:
            results.append(CheckResult(name, Status.SKIP, str(skip)))
        except _Failed as failure:
            results.append(CheckResult(name, Status.FAIL, str(failure)))
        except Exception as exc:
            results.append(CheckResult(name, Status.FAIL, f"check raised {_describe(exc)}"))
    return results


async def run_conformance(target: ConformanceTarget) -> list[CheckResult]:
    """Run every check against ``target``.

    Never raises for a non-conforming or unreachable server — that is a ``FAIL``
    result.
    """
    logging.getLogger("mcp").setLevel(logging.CRITICAL)  # SDK logs carry provider bytes
    token = target.token.get_secret_value() if target.token else None
    try:
        async with asyncio.timeout(target.timeout_seconds * 4):
            async with _open(target.url, token, target.timeout_seconds) as (sdk, counting):
                results = await _run_checks(_Run(sdk, counting, target))
    except Exception as exc:
        return [CheckResult("connect", Status.FAIL, f"could not open a session: {_describe(exc)}")]
    results.append(await _auth_rejects_bad_credentials(target))
    return results


# --- CLI -------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m adapter.product_knowledge.conformance",
        description="Check an MCP endpoint against the product-knowledge contract v1. "
        "The bearer token is read from the PK_TOKEN environment variable only.",
    )
    parser.add_argument("--pk-url", default=os.environ.get("PK_URL"), help="MCP endpoint URL")
    parser.add_argument(
        "--pk-query",
        default=os.environ.get("PK_QUERY", DEFAULT_QUERY),
        help="a search term the endpoint answers with at least one result",
    )
    args = parser.parse_args(argv)
    if not args.pk_url:
        parser.error("--pk-url (or PK_URL) is required")
    token = os.environ.get("PK_TOKEN", "").strip()
    target = ConformanceTarget(
        url=args.pk_url, token=SecretStr(token) if token else None, query=args.pk_query
    )
    results = asyncio.run(run_conformance(target))
    for r in results:
        print(f"{r.status.value.upper():4} {r.name}: {r.detail}")
    failed = [r for r in results if r.status is Status.FAIL]
    print(f"{len(results) - len(failed)} of {len(results)} checks passed or skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
