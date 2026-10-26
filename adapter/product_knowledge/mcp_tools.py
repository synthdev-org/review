"""
adapter.product_knowledge.mcp_tools: consult one nominated MCP server with a generic tool call.

Local mode's MCP knowledge (ADR-0017, plan Phase 7). The product-knowledge client in ``client.py``
speaks one four-tool contract over Streamable HTTP only; a developer's own servers speak neither.
This module reuses its pieces and swaps only what must differ:

* the SDK ``Client`` in ``mode="auto"`` and ``ClientTimeouts`` (connect, call, teardown grace) are
  the same, as are the failure classifiers (``exception_leaves``, ``network_outcome``,
  ``is_malformed``);
* HTTP goes through ``GuardedTransport`` (no redirect is ever followed, the resolved address is
  pinned, no environment proxy, a response cap), subclassed twice: to attach the developer's own
  configured headers, only to the entry's own scheme+host+port and never in cleartext to a public
  address, and to allow loopback for a server whose configured host is literally ``localhost``,
  ``127.0.0.1`` or ``::1`` (link-local, metadata, unspecified and multicast stay denied). Neither
  is a change to the server-mode guard;
* stdio is new: ``bounded_stdio`` spawns the command with an allow-list environment and an empty
  temporary working directory, counts the bytes read from the child as they arrive (the cap is
  enforced on the stream, not after the fact) and always kills the process group and reaps it.

A consultation is exactly one session: connect, ``list_tools`` (at most ``max_tools`` tools and
``max_list_tools_bytes`` of them are looked at), pick one tool by the deterministic rule in
``choose_tool``, one ``call_tool`` with the one argument that tool declares for a query, and the
text blocks of the reply. Tool descriptions are never read for behaviour; a tool's
read-only, destructive and open-world annotations are read only to decide eligibility
(``choose_tool``). There is no retry. Anything that goes wrong is an ``McpConsultError`` carrying a
closed ``McpFailure``; provider text, headers and environment values are in no message.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

import anyio
import httpx2
import mcp_types as types
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.os.posix.utilities import terminate_posix_process_tree
from mcp.shared.message import SessionMessage

from adapter.product_knowledge.client import (
    ClientTimeouts,
    exception_leaves,
    is_malformed,
    network_outcome,
)
from adapter.product_knowledge.egress import AddressClass, canonicalize_host, classify_address
from adapter.product_knowledge.transport import (
    EgressPolicy,
    GuardedTransport,
    Resolver,
    build_http_client,
    system_resolver,
)
from review.product_knowledge import ProviderOutcome

__all__ = [
    "LOOPBACK_HOSTS",
    "HttpTarget",
    "McpConsultError",
    "McpConsultation",
    "McpFailure",
    "McpLimits",
    "McpToolClient",
    "StdioTarget",
    "choose_tool",
]

# The only hosts a configured entry may name to be allowed to resolve to loopback.
LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})

SEARCH_WORDS: Final = ("search", "query", "lookup")
_QUERY_ARGUMENTS: Final = ("query", "q", "search", "text", "keywords", "prompt")
_READ_CHUNK: Final = 65_536
# Environment a stdio child may inherit (ADR-0017 decision 6); everything else is absent.
_INHERITED: Final = ("PATH", "LANG", "LC_ALL")


class McpFailure(StrEnum):
    """How a consultation failed. The values are what the run summary prints."""

    TIMEOUT = "timeout"
    PROTOCOL_ERROR = "protocol error"
    MALFORMED = "not valid MCP output"
    NO_USABLE_TOOL = "no usable tool"
    EGRESS_REFUSED = "refused by the egress policy"
    TOO_LARGE = "output over the size cap"
    START_FAILED = "could not be started"


class McpConsultError(Exception):
    """A consultation failed. Carries only the closed failure, never server text."""

    def __init__(self, failure: McpFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


@dataclass(frozen=True)
class StdioTarget:
    command: str
    args: tuple[str, ...] = ()
    # Values may be credentials: kept out of repr, never logged or put in a message.
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def transport(self) -> str:
        return "stdio"


@dataclass(frozen=True)
class HttpTarget:
    url: str = field(repr=False)  # may embed credentials
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def transport(self) -> str:
        return "http"


@dataclass(frozen=True)
class McpLimits:
    """Caps on one consultation (ADR-0017 decision 7); every value is a setting at the root."""

    max_response_bytes: int = 262_144
    max_list_tools_bytes: int = 65_536
    max_tools: int = 100
    max_text_blocks: int = 3
    max_block_chars: int = 16_000
    timeouts: ClientTimeouts = field(default_factory=ClientTimeouts)
    kill_grace_seconds: float = 1.0


@dataclass(frozen=True)
class McpConsultation:
    tool: str
    texts: tuple[str, ...]


# --- tool choice -------------------------------------------------------------------------------


def _string_properties(schema: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {
        name: spec
        for name, spec in properties.items()
        if isinstance(name, str)
        and isinstance(spec, dict)
        and spec.get("type", "string") == "string"
    }


def _query_argument(schema: Mapping[str, Any]) -> str | None:
    """The one argument a query goes in: the first of ``_QUERY_ARGUMENTS`` the tool declares as a
    string. Never any other parameter (``sql``, ``command``, ``expression`` ...): diff-derived text
    must not become code a server executes."""
    strings = _string_properties(schema)
    return next((name for name in _QUERY_ARGUMENTS if name in strings), None)


def _is_read_only(tool: types.Tool) -> bool:
    """The server declares the tool read-only, and neither destructive nor open-world."""
    hints = tool.annotations
    return (
        hints is not None
        and hints.read_only_hint is True
        and hints.destructive_hint is not True
        and hints.open_world_hint is not True
    )


def choose_tool(
    tools: list[types.Tool], wanted: str | None, *, tool_trusted: bool = False
) -> tuple[str, str]:
    """``(tool name, argument name)``.

    A tool is eligible only if the server declares it read-only (``readOnlyHint`` true, not
    ``destructiveHint`` or ``openWorldHint``) or the developer named it themselves (``wanted`` with
    ``tool_trusted``: ``--mcp NAME:TOOL``; a name read from a repository file is not trusted). With
    ``wanted``, exactly that tool; otherwise the single eligible tool whose name contains
    ``search``, ``query`` or ``lookup``: none or several is ``NO_USABLE_TOOL``, never a guess. The
    query goes only into a parameter named for a query, and a tool that requires any other
    parameter cannot be called with only a query and is not usable either."""
    if wanted is not None:
        matches = [t for t in tools if t.name == wanted and (tool_trusted or _is_read_only(t))]
    else:
        matches = [
            t
            for t in tools
            if any(word in t.name.lower() for word in SEARCH_WORDS) and _is_read_only(t)
        ]
    if len(matches) != 1:
        raise McpConsultError(McpFailure.NO_USABLE_TOOL)
    schema = matches[0].input_schema
    argument = _query_argument(schema)
    required = schema.get("required")
    others = {str(r) for r in required if r != argument} if isinstance(required, list) else set()
    if argument is None or others:
        raise McpConsultError(McpFailure.NO_USABLE_TOOL)
    return matches[0].name, argument


# --- HTTP ---------------------------------------------------------------------------------------


class _LocalGuardedTransport(GuardedTransport):
    """``GuardedTransport`` plus the developer's configured headers and the loopback allowance.

    Every check in ``handle_async_request`` (target is the one bound origin, allow-list, address
    classification, redirects refused, response cap) is inherited; the two hooks below only add to
    it, after the target check has already passed.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        allow_loopback: bool,
        policy: EgressPolicy,
        resolver: Resolver,
        inner: httpx2.AsyncBaseTransport | None,
        max_response_bytes: int,
    ) -> None:
        super().__init__(
            url,
            policy=policy,
            resolver=resolver,
            inner=inner,
            max_response_bytes=max_response_bytes,
        )
        self._configured_headers = dict(headers)
        self._allow_loopback = allow_loopback

    def _outbound_headers(
        self,
        request: httpx2.Request,
        scheme: str,
        host: str,
        port: int,
        classes: list[AddressClass],
    ) -> httpx2.Headers:
        out = super()._outbound_headers(request, scheme, host, port, classes)
        if self._configured_headers:
            if scheme == "http" and AddressClass.PUBLIC in classes:
                raise self._refuse(
                    ProviderOutcome.BLOCKED_EGRESS, "credentials refused over cleartext http"
                )
            for name, value in self._configured_headers.items():
                out[name] = value
        return out

    async def _resolve(self, host: str, port: int) -> tuple[str, list[AddressClass]]:
        if not self._allow_loopback:
            return await super()._resolve(host, port)
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            try:
                addresses = await self._resolver(host, port)
            except OSError as exc:
                raise httpx2.ConnectError("name resolution failed") from exc
        if not addresses:
            raise httpx2.ConnectError("name resolution returned no address")
        # A loopback answer for a host that is literally a loopback name is the one relaxation;
        # any other address in the answer is classified as usual.
        classes = [
            AddressClass.PRIVATE if ipaddress.ip_address(a).is_loopback else classify_address(a)
            for a in addresses
        ]
        return addresses[0], classes


# --- stdio --------------------------------------------------------------------------------------


@dataclass
class _StdioWatch:
    """What the stdio reader saw, for classifying a failure afterwards."""

    breached: bool = False
    garbled: bool = False


@dataclass
class StdioRuntime:
    """Process-level state a stdio consultation needs, created by the composition root and passed in
    (architecture.md: adapters read no environment and keep no module-level state).

    ``inherited_env`` is the environment the root chose to make available (normally a copy of its
    own); only ``_INHERITED`` names are ever passed on. ``teardowns`` holds stops still finishing
    after their caller was cancelled (a strong reference, or the task could be collected mid-way).
    """

    inherited_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    teardowns: set[asyncio.Future[None]] = field(default_factory=set, repr=False)


def child_environment(
    target: StdioTarget, workdir: str, inherited: Mapping[str, str]
) -> dict[str, str]:
    """The child's whole environment: PATH, LANG and LC_ALL if set in ``inherited``, HOME as the
    empty working directory, and only the variables the developer's own config lists for this
    server."""
    env = {name: inherited[name] for name in _INHERITED if name in inherited}
    env["HOME"] = workdir
    env.update(target.env)
    return env


async def _wait_exit(process: anyio.abc.Process, seconds: float) -> bool:
    deadline = anyio.current_time() + seconds
    while process.returncode is None:
        if anyio.current_time() >= deadline:
            return False
        await anyio.sleep(0.01)
    return True


async def _stop(process: anyio.abc.Process, grace: float) -> None:
    """Close stdin, give the server a moment, then kill its whole process group and reap it."""
    with suppress(OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
        if process.stdin is not None:
            await process.stdin.aclose()
    if not await _wait_exit(process, grace):
        await terminate_posix_process_tree(process, grace)
    # Also when the main process already exited on its own: it may have left children in the group.
    _kill_group(process)
    with anyio.move_on_after(2.0, shield=True):
        await process.aclose()  # closes the pipes and waits: no zombie is left behind


def _kill_group(process: anyio.abc.Process) -> None:
    """SIGKILL the child's whole process group. Synchronous on purpose: nothing can interrupt it.

    Once the leader has been reaped its pid is free to be reused, but not while a member of its
    group is alive. So if a process with that pid exists now it is not ours (we would be
    killing a stranger's group), and the sweep is skipped; if none does, any member left is ours.
    """
    pid = process.pid
    if process.returncode is not None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            return
        else:
            return
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


async def _teardown(process: anyio.abc.Process, grace: float, runtime: StdioRuntime) -> None:
    """Stop the child even when the caller is cancelled again while it is stopping.

    ``asyncio.Task.cancel`` is not stopped by an anyio shield, so the polite stop runs as its own
    task the caller awaits through ``asyncio.shield``. If the caller is cancelled (the overall
    deadline firing during teardown) or the stop fails, the group is killed on the spot; the stop
    task finishes reaping in the background.
    """
    stopping = asyncio.ensure_future(_stop(process, grace))
    runtime.teardowns.add(stopping)
    stopping.add_done_callback(runtime.teardowns.discard)
    try:
        await asyncio.shield(stopping)
    finally:
        # A stop that finished cleanly already killed the group; killing again after the reap
        # could hit a recycled pid. Otherwise (cancelled mid-stop, or it failed) kill now.
        if not stopping.done() or stopping.cancelled() or stopping.exception() is not None:
            _kill_group(process)


@asynccontextmanager
async def bounded_stdio(
    target: StdioTarget, limits: McpLimits, watch: _StdioWatch, runtime: StdioRuntime
) -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """An MCP stdio transport with a bounded reader and a guaranteed kill-and-reap."""
    workdir = await asyncio.to_thread(tempfile.mkdtemp, prefix="review-mcp-")
    try:
        try:
            process = await anyio.open_process(
                [target.command, *target.args],
                env=child_environment(target, workdir, runtime.inherited_env),
                stderr=subprocess.DEVNULL,
                cwd=workdir,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise McpConsultError(McpFailure.START_FAILED) from exc
        if process.stdout is None or process.stdin is None:  # not piped: nothing to speak over
            await _stop(process, 0.0)
            raise McpConsultError(McpFailure.START_FAILED)
        stdout, stdin = process.stdout, process.stdin
        read_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
        write_stream, write_reader = anyio.create_memory_object_stream[SessionMessage](0)

        async def reader() -> None:
            total = 0
            buffer = b""
            try:
                async with read_writer:
                    while True:
                        try:
                            chunk = await stdout.receive(_READ_CHUNK)
                        except anyio.EndOfStream:
                            return
                        total += len(chunk)
                        if total > limits.max_response_bytes:
                            watch.breached = True
                            return  # closing the stream ends the session; the child is killed
                        *lines, buffer = (buffer + chunk).split(b"\n")
                        for line in lines:
                            if not line.strip():
                                continue
                            try:
                                message = types.jsonrpc_message_adapter.validate_json(
                                    line, by_name=False
                                )
                            except ValueError:
                                watch.garbled = True
                                continue
                            await read_writer.send(SessionMessage(message))
            except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
                return

        async def writer() -> None:
            try:
                async with write_reader:
                    async for session_message in write_reader:
                        data = session_message.message.model_dump_json(
                            by_alias=True, exclude_unset=True
                        )
                        await stdin.send(data.encode("utf-8") + b"\n")
            except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
                await read_writer.aclose()

        async with anyio.create_task_group() as group:
            group.start_soon(reader)
            group.start_soon(writer)
            try:
                yield read_stream, write_stream
            finally:
                read_stream.close()
                write_stream.close()
                await _teardown(process, limits.kill_grace_seconds, runtime)
                group.cancel_scope.cancel()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --- the client ---------------------------------------------------------------------------------


class McpToolClient:
    """One nominated server, consulted at most once per ``consult`` call."""

    def __init__(
        self,
        target: StdioTarget | HttpTarget,
        *,
        limits: McpLimits | None = None,
        process_slots: asyncio.Semaphore | None = None,
        runtime: StdioRuntime | None = None,
        resolver: Resolver = system_resolver,
        inner_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._target = target
        self._limits = limits or McpLimits()
        self._slots = process_slots
        self._runtime = runtime or StdioRuntime()
        self._resolver = resolver
        self._inner = inner_transport

    @property
    def transport(self) -> str:
        return self._target.transport

    async def consult(
        self, *, query: str, tool: str | None, tool_trusted: bool = False
    ) -> McpConsultation:
        """Connect, list tools, call the chosen one once with ``query``, return its text blocks.
        ``tool_trusted``: the developer themselves named ``tool`` (see ``choose_tool``)."""
        if isinstance(self._target, StdioTarget):
            slots = self._slots
            if slots is None:
                return await self._consult_stdio(self._target, query, tool, tool_trusted)
            async with slots:
                return await self._consult_stdio(self._target, query, tool, tool_trusted)
        return await self._consult_http(self._target, query, tool, tool_trusted)

    async def _consult_stdio(
        self, target: StdioTarget, query: str, tool: str | None, trusted: bool
    ) -> McpConsultation:
        if sys.platform == "win32":
            # The process-group supervision below is POSIX-only (os.killpg, mcp.os.posix); better a
            # clean refusal than a half-supervised child.
            raise McpConsultError(McpFailure.START_FAILED)
        watch = _StdioWatch()
        try:
            return await self._session(
                bounded_stdio(target, self._limits, watch, self._runtime), query, tool, trusted
            )
        except McpConsultError:
            raise
        except Exception as exc:
            raise McpConsultError(self._classify_stdio(exc, watch)) from exc

    def _classify_stdio(self, exc: BaseException, watch: _StdioWatch) -> McpFailure:
        leaves = list(exception_leaves(exc))
        for leaf in leaves:
            if isinstance(leaf, McpConsultError):
                return leaf.failure
        if watch.breached:
            return McpFailure.TOO_LARGE
        if watch.garbled or is_malformed(leaves):
            return McpFailure.MALFORMED
        if any(isinstance(e, TimeoutError) for e in leaves):
            return McpFailure.TIMEOUT
        return McpFailure.PROTOCOL_ERROR

    async def _consult_http(
        self, target: HttpTarget, query: str, tool: str | None, trusted: bool
    ) -> McpConsultation:
        from urllib.parse import urlsplit

        try:
            parts = urlsplit(target.url)
            host = canonicalize_host(parts.hostname or "")
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError as exc:
            raise McpConsultError(McpFailure.EGRESS_REFUSED) from exc
        if host is None or parts.scheme not in ("http", "https"):
            raise McpConsultError(McpFailure.EGRESS_REFUSED)
        # The entry names its own host, so that host is the (one-entry) allow-list.
        policy = EgressPolicy(
            allow_private_networks=True,
            endpoint_allowlist=(f"[{host}]:{port}" if ":" in host else f"{host}:{port}",),
        )
        guard = _LocalGuardedTransport(
            target.url,
            headers=target.headers,
            allow_loopback=host in LOOPBACK_HOSTS,
            policy=policy,
            resolver=self._resolver,
            inner=self._inner,
            max_response_bytes=self._limits.max_response_bytes,
        )
        try:
            async with build_http_client(guard) as http:
                return await self._session(
                    streamable_http_client(target.url, http_client=http), query, tool, trusted
                )
        except McpConsultError:
            raise
        except Exception as exc:
            raise McpConsultError(self._classify_http(exc, guard)) from exc

    def _classify_http(self, exc: BaseException, guard: GuardedTransport) -> McpFailure:
        leaves = list(exception_leaves(exc))
        for leaf in leaves:
            if isinstance(leaf, McpConsultError):
                return leaf.failure
        refused = guard.outcome
        if refused in (ProviderOutcome.BLOCKED_EGRESS, ProviderOutcome.CREDENTIAL_UNAVAILABLE):
            return McpFailure.EGRESS_REFUSED
        if refused is ProviderOutcome.OVERSIZED:
            return McpFailure.TOO_LARGE
        if refused is ProviderOutcome.MALFORMED or is_malformed(leaves):
            return McpFailure.MALFORMED
        if network_outcome(leaves) is ProviderOutcome.TIMEOUT:
            return McpFailure.TIMEOUT
        return McpFailure.PROTOCOL_ERROR

    async def _session(
        self, transport: Any, query: str, tool: str | None, trusted: bool
    ) -> McpConsultation:
        timeouts = self._limits.timeouts
        # One wall-clock deadline around the whole session including teardown: a server that hangs
        # or drips must not outlive it (httpx read timeouts are per read).
        async with asyncio.timeout(timeouts.session_seconds), AsyncExitStack() as stack:
            sdk = Client(transport, mode="auto", cache=None)
            async with asyncio.timeout(timeouts.connect_seconds):
                await stack.enter_async_context(sdk)
            async with asyncio.timeout(timeouts.call_seconds):
                return await self._consult(sdk, query, tool, trusted)

    async def _consult(
        self, sdk: Client, query: str, wanted: str | None, trusted: bool
    ) -> McpConsultation:
        limits = self._limits
        tools = (await sdk.list_tools()).tools[: limits.max_tools]
        if sum(len(t.model_dump_json().encode()) for t in tools) > limits.max_list_tools_bytes:
            raise McpConsultError(McpFailure.TOO_LARGE)
        name, argument = choose_tool(tools, wanted, tool_trusted=trusted)
        result = await sdk.session.call_tool(name, {argument: query}, allow_input_required=True)
        if isinstance(result, types.InputRequiredResult) or result.is_error:
            raise McpConsultError(McpFailure.PROTOCOL_ERROR)
        texts = tuple(
            block.text[: limits.max_block_chars]
            for block in result.content
            if isinstance(block, types.TextContent)
        )[: limits.max_text_blocks]
        return McpConsultation(tool=name, texts=texts)
