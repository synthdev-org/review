"""A small respx cassette for the real ``LiteLLMClient`` wire path.

Replay matches each outgoing ``/chat/completions`` request body against the
committed interactions; a request with no match (a prompt change left the
cassette stale) or an interaction never requested raises ``CassetteStaleError``
naming the re-record command. No headers are ever stored (only request and
response bodies), so ``Authorization``/cookie values cannot reach a committed
file.

LiteLLM defaults to an aiohttp transport that respx cannot see; it is
switched to httpx for the duration of a mock.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import litellm
import respx
from pydantic import BaseModel

Produce = Callable[[httpx.Request], Awaitable[tuple[int, dict[str, Any]]]]


class CassetteStaleError(AssertionError):
    pass


class Interaction(BaseModel):
    request: dict[str, Any]
    status: int
    response: dict[str, Any]


class Cassette(BaseModel):
    synthetic: bool
    model: str
    note: str
    rerecord: str
    interactions: list[Interaction]


def canonical(body: Any) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def load_cassette(path: Path) -> Cassette:
    return Cassette.model_validate_json(path.read_text())


def save_cassette(path: Path, cassette: Cassette) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cassette.model_dump(), indent=2, sort_keys=True) + "\n")


@contextmanager
def _mock(handler: Callable[[httpx.Request], Awaitable[httpx.Response]]) -> Iterator[None]:
    previous = litellm.disable_aiohttp_transport
    litellm.disable_aiohttp_transport = True
    try:
        with respx.mock(assert_all_called=False) as router:
            router.post(url__regex=r".*/chat/completions$").mock(side_effect=handler)
            yield
    finally:
        litellm.disable_aiohttp_transport = previous


@contextmanager
def replay(cassette: Cassette) -> Iterator[None]:
    """Serve ``cassette``'s interactions; raise ``CassetteStaleError`` on a
    request with no match or on an interaction left unused.
    """
    by_request = {canonical(i.request): i for i in cassette.interactions}
    used: set[str] = set()
    unmatched: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        key = canonical(json.loads(request.content))
        interaction = by_request.get(key)
        if interaction is None:
            unmatched.append(key[:200])
            return httpx.Response(599, json={"error": "no matching cassette interaction"})
        used.add(key)
        return httpx.Response(interaction.status, json=interaction.response)

    failure: BaseException | None = None
    try:
        with _mock(handle):
            yield
    except BaseException as exc:
        failure = exc
        raise
    finally:
        problems = []
        if unmatched:
            problems.append(f"{len(unmatched)} request(s) matched no interaction")
        if failure is None and (unused := set(by_request) - used):
            problems.append(f"{len(unused)} interaction(s) were never requested")
        if problems:
            raise CassetteStaleError(
                f"cassette is stale ({'; '.join(problems)}). "
                f"A prompt change needs a deliberate re-record: {cassette.rerecord}"
            ) from failure


def _forward(request: httpx.Request) -> tuple[int, dict[str, Any]]:
    # stdlib on purpose: respx patches httpx/httpcore, so an httpx client here would recurse.
    req = urllib.request.Request(
        str(request.url), data=request.content, method=request.method, headers=dict(request.headers)
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:  # nosec B310 - URL is the recorded http(s) request
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


async def forward_to_endpoint(request: httpx.Request) -> tuple[int, dict[str, Any]]:
    """Produce function for a real recording: sends the request on."""
    return await asyncio.to_thread(_forward, request)


@contextmanager
def record(produce: Produce, interactions: list[Interaction]) -> Iterator[None]:
    """Answer each request via ``produce`` (a real endpoint, or a synthetic
    responder) and append the exchange to ``interactions``. Request headers
    are never stored.
    """

    async def handle(request: httpx.Request) -> httpx.Response:
        status, body = await produce(request)
        interactions.append(
            Interaction(request=json.loads(request.content), status=status, response=body)
        )
        return httpx.Response(status, json=body)

    with _mock(handle):
        yield
