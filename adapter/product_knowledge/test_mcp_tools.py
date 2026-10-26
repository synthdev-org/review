"""Integration tests for the generic nominated-server client (plan Phase 7, ADR-0017).

A real MCP server built on the official SDK (``testing/local_mcp_stub.py``) is spawned as a child
process (stdio) or served on a loopback port (Streamable HTTP), and the real client talks to it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx2
import mcp_types as types
import pytest

from adapter.product_knowledge.client import ClientTimeouts
from adapter.product_knowledge.mcp_tools import (
    HttpTarget,
    McpConsultError,
    McpFailure,
    McpLimits,
    McpToolClient,
    StdioRuntime,
    StdioTarget,
    child_environment,
    choose_tool,
)
from adapter.product_knowledge.testing.local_mcp_harness import (
    calls,
    http_stub,
    read_log,
    stdio_target,
)
from adapter.product_knowledge.testing.local_mcp_stub import FACT, HOSTILE

pytestmark = pytest.mark.integration

_FAST = McpLimits(timeouts=ClientTimeouts(connect_seconds=8.0, call_seconds=8.0))
_PLANTED = "ghp_PLANTED_TOKEN_must_not_reach_a_child_0123456789"


_READ_ONLY = types.ToolAnnotations(read_only_hint=True)


def _tool(
    name: str,
    schema: dict[str, object],
    *,
    hints: types.ToolAnnotations | None = _READ_ONLY,
) -> types.Tool:
    return types.Tool(name=name, input_schema={"type": "object", **schema}, annotations=hints)


def _requests(log: Path) -> list[dict[str, Any]]:
    return [e for e in read_log(log) if e["event"] == "request"]


async def _gone(pid: int) -> bool:
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)  # the loop must run: it is what reaps the child
    return False


async def _failure(client: McpToolClient, query: str = "charge") -> McpFailure:
    with pytest.raises(McpConsultError) as caught:
        await client.consult(query=query, tool=None)
    return caught.value.failure


# --- tool choice (pure) ------------------------------------------------------------------------


def test_the_single_search_like_tool_is_chosen_and_gets_the_declared_query_argument() -> None:
    tools = [
        _tool("get_page", {"properties": {"id": {"type": "string"}}}),
        _tool(
            "search_docs", {"properties": {"q": {"type": "string"}, "limit": {"type": "integer"}}}
        ),
    ]

    assert choose_tool(tools, None) == ("search_docs", "q")


@pytest.mark.parametrize(
    "tools",
    [
        [_tool("get_page", {"properties": {"id": {"type": "string"}}})],
        [
            _tool("search_docs", {"properties": {"query": {"type": "string"}}}),
            _tool("lookup_wiki", {"properties": {"query": {"type": "string"}}}),
        ],
        [
            _tool(
                "search_docs",
                {"properties": {"query": {"type": "string"}}, "required": ["query", "space"]},
            )
        ],
        [_tool("search_docs", {"properties": {"limit": {"type": "integer"}}})],
    ],
    ids=["none", "several-never-guessed", "needs-another-argument", "no-string-argument"],
)
def test_no_tool_is_guessed(tools: list[types.Tool]) -> None:
    with pytest.raises(McpConsultError) as caught:
        choose_tool(tools, None)

    assert caught.value.failure is McpFailure.NO_USABLE_TOOL


def test_a_named_tool_is_used_even_when_its_name_is_not_search_like() -> None:
    tools = [_tool("fetch_policy", {"properties": {"text": {"type": "string"}}})]

    assert choose_tool(tools, "fetch_policy", tool_trusted=True) == ("fetch_policy", "text")
    with pytest.raises(McpConsultError):
        choose_tool(tools, "no_such_tool", tool_trusted=True)


def test_the_child_environment_is_an_allow_list_plus_the_servers_own_variables() -> None:
    inherited = {
        "GITHUB_TOKEN": _PLANTED,
        "REVIEW_LOCAL_LLM_API_KEY": _PLANTED,
        "LANG": "C.UTF-8",
    }

    env = child_environment(
        StdioTarget("x", (), {"DOCS_KEY": "from-config"}), "/tmp/empty", inherited
    )

    assert set(env) <= {"PATH", "LANG", "LC_ALL", "HOME", "DOCS_KEY"}
    assert env["LANG"] == "C.UTF-8"
    assert env["HOME"] == "/tmp/empty" and env["DOCS_KEY"] == "from-config"
    assert _PLANTED not in env.values()


# --- stdio ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stdio_server_is_consulted_once_and_its_fact_depends_on_the_query(
    tmp_path: Path,
) -> None:
    log = tmp_path / "stub.log"
    client = McpToolClient(stdio_target(log), limits=_FAST)

    hit = await client.consult(query="payments/charge.py charge_card", tool=None)
    miss = await client.consult(query="docs/readme.md", tool=None)

    assert hit.texts == (FACT,) and hit.tool == "search_docs"
    assert miss.texts == ("No matching documents.",)
    assert [c["query"] for c in calls(log)] == ["payments/charge.py charge_card", "docs/readme.md"]


@pytest.mark.asyncio
async def test_the_child_gets_a_scrubbed_environment_and_an_empty_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", _PLANTED)
    monkeypatch.setenv("GH_TOKEN", _PLANTED)
    monkeypatch.setenv("REVIEW_LOCAL_LLM_API_KEY", _PLANTED)
    log = tmp_path / "stub.log"
    target = stdio_target(log, env={"DOCS_API_KEY": "configured-for-this-server"})

    await McpToolClient(
        target, limits=_FAST, runtime=StdioRuntime(inherited_env=os.environ)
    ).consult(query="charge", tool=None)

    start = next(e for e in read_log(log) if e["event"] == "start")
    assert _PLANTED not in str(start["env"])
    assert set(start["env"]) - {"PATH", "LANG", "LC_ALL", "HOME", "DOCS_API_KEY"} <= {
        "LC_CTYPE",  # set by the interpreter itself on start
        "PYTHONHASHSEED",
    }
    assert start["env"]["DOCS_API_KEY"] == "configured-for-this-server"
    assert start["cwd"] == start["env"]["HOME"] != str(Path.cwd())
    assert not Path(start["cwd"]).exists()  # the temporary directory is removed afterwards


@pytest.mark.asyncio
async def test_the_process_is_gone_after_a_consultation_and_after_a_timeout(tmp_path: Path) -> None:
    ok_log, hang_log = tmp_path / "ok.log", tmp_path / "hang.log"
    await McpToolClient(stdio_target(ok_log), limits=_FAST).consult(query="charge", tool=None)
    slow = McpLimits(timeouts=ClientTimeouts(connect_seconds=8.0, call_seconds=0.7))

    failure = await _failure(McpToolClient(stdio_target(hang_log, "hang"), limits=slow))

    assert failure is McpFailure.TIMEOUT
    for log in (ok_log, hang_log):
        assert await _gone(next(e for e in read_log(log) if e["event"] == "start")["pid"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("crash", McpFailure.PROTOCOL_ERROR),
        ("error", McpFailure.PROTOCOL_ERROR),
        ("garbage", McpFailure.MALFORMED),
        ("huge", McpFailure.TOO_LARGE),
        ("two_tools", McpFailure.NO_USABLE_TOOL),
        ("no_tool", McpFailure.NO_USABLE_TOOL),
    ],
)
async def test_every_stdio_failure_is_a_closed_reason(
    tmp_path: Path, mode: str, expected: McpFailure
) -> None:
    log = tmp_path / "stub.log"
    limits = McpLimits(max_response_bytes=262_144, timeouts=ClientTimeouts(3.0, 3.0))

    failure = await _failure(McpToolClient(stdio_target(log, mode), limits=limits))

    assert failure is expected
    starts = [e for e in read_log(log) if e["event"] == "start"]
    assert starts and await _gone(starts[0]["pid"])


@pytest.mark.asyncio
async def test_a_command_that_does_not_exist_is_a_start_failure(tmp_path: Path) -> None:
    client = McpToolClient(StdioTarget(str(tmp_path / "no-such-server")), limits=_FAST)

    assert await _failure(client) is McpFailure.START_FAILED


@pytest.mark.asyncio
async def test_an_empty_result_is_returned_as_no_text(tmp_path: Path) -> None:
    result = await McpToolClient(stdio_target(tmp_path / "s.log", "empty"), limits=_FAST).consult(
        query="charge", tool=None
    )

    assert all(not t.strip() for t in result.texts)


@pytest.mark.asyncio
async def test_hostile_content_is_returned_as_data_and_causes_no_further_call(
    tmp_path: Path,
) -> None:
    log = tmp_path / "stub.log"

    result = await McpToolClient(stdio_target(log, "hostile"), limits=_FAST).consult(
        query="charge", tool=None
    )

    assert result.texts == (HOSTILE[:16_000],)
    assert [c["tool"] for c in calls(log)] == ["search_docs"]  # delete_everything was never called


@pytest.mark.asyncio
async def test_the_list_of_tools_is_capped(tmp_path: Path) -> None:
    limits = McpLimits(max_list_tools_bytes=10, timeouts=ClientTimeouts(5.0, 5.0))

    failure = await _failure(McpToolClient(stdio_target(tmp_path / "s.log"), limits=limits))

    assert failure is McpFailure.TOO_LARGE


# --- Streamable HTTP --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_http_server_on_a_literal_loopback_host_is_consulted_with_its_own_headers(
    tmp_path: Path,
) -> None:
    with http_stub(tmp_path / "http.log") as stub:
        target = HttpTarget(stub.url, {"X-Api-Key": "configured-secret"})

        result = await McpToolClient(target, limits=_FAST).consult(query="charge", tool=None)

        assert result.texts == (FACT,)
        requests = _requests(stub.log)
        assert requests and all(r["headers"]["x-api-key"] == "configured-secret" for r in requests)


@pytest.mark.asyncio
async def test_http_loopback_is_refused_for_a_host_that_is_not_literally_a_loopback_name(
    tmp_path: Path,
) -> None:
    async def resolves_to_loopback(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]

    with http_stub(tmp_path / "http.log") as stub:
        url = stub.url.replace("127.0.0.1", "docs.example.test")
        client = McpToolClient(HttpTarget(url), limits=_FAST, resolver=resolves_to_loopback)

        assert await _failure(client) is McpFailure.EGRESS_REFUSED
        assert _requests(stub.log) == []  # the stub never saw a request


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["169.254.169.254", "100.100.100.200", "0.0.0.0", "224.0.0.1"])
async def test_link_local_metadata_unspecified_and_multicast_stay_blocked_everywhere(
    address: str,
) -> None:
    async def resolver(host: str, port: int) -> list[str]:
        return [address]

    client = McpToolClient(HttpTarget("http://localhost:9/mcp"), limits=_FAST, resolver=resolver)

    assert await _failure(client) is McpFailure.EGRESS_REFUSED


@pytest.mark.asyncio
async def test_a_redirect_is_never_followed_and_the_configured_header_does_not_cross_it(
    tmp_path: Path,
) -> None:
    with (
        http_stub(tmp_path / "elsewhere.log") as elsewhere,
        http_stub(tmp_path / "origin.log", "redirect", redirect_to=elsewhere.url) as origin,
    ):
        target = HttpTarget(origin.url, {"Authorization": "Bearer configured-secret"})

        failure = await _failure(McpToolClient(target, limits=_FAST))

        assert failure is McpFailure.EGRESS_REFUSED
        assert _requests(elsewhere.log) == []  # nothing, so no header either


@pytest.mark.asyncio
async def test_an_http_server_error_is_a_protocol_error(tmp_path: Path) -> None:
    with http_stub(tmp_path / "http.log", "crash") as stub:
        assert await _failure(McpToolClient(HttpTarget(stub.url), limits=_FAST)) is (
            McpFailure.PROTOCOL_ERROR
        )


@pytest.mark.asyncio
async def test_an_http_server_that_hangs_times_out(tmp_path: Path) -> None:
    limits = McpLimits(timeouts=ClientTimeouts(connect_seconds=8.0, call_seconds=0.7))
    with http_stub(tmp_path / "http.log", "hang") as stub:
        assert await _failure(McpToolClient(HttpTarget(stub.url), limits=limits)) is (
            McpFailure.TIMEOUT
        )


@pytest.mark.asyncio
async def test_credentials_are_refused_over_cleartext_http_to_a_public_address() -> None:
    async def public(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    class _Unreachable(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            raise AssertionError("a request with credentials reached the wire")

    client = McpToolClient(
        HttpTarget("http://docs.example.test/mcp", {"Authorization": "Bearer secret"}),
        limits=_FAST,
        resolver=public,
        inner_transport=_Unreachable(),
    )

    assert await _failure(client) is McpFailure.EGRESS_REFUSED


@pytest.mark.asyncio
async def test_the_process_cap_runs_children_one_at_a_time_when_the_cap_is_one(
    tmp_path: Path,
) -> None:
    slots = asyncio.Semaphore(1)
    slow = McpLimits(timeouts=ClientTimeouts(connect_seconds=8.0, call_seconds=0.8))
    logs = [tmp_path / "a.log", tmp_path / "b.log"]
    clients = [
        McpToolClient(stdio_target(log, "hang"), limits=slow, process_slots=slots) for log in logs
    ]

    results = await asyncio.gather(
        *(c.consult(query="charge", tool=None) for c in clients), return_exceptions=True
    )

    assert all(isinstance(r, McpConsultError) for r in results)
    first, second = sorted(
        next(e["time"] for e in read_log(log) if e["event"] == "start") for log in logs
    )
    assert second - first >= 0.7  # the second child started only after the first was gone
    uncapped = [tmp_path / "c.log", tmp_path / "d.log"]
    await asyncio.gather(
        *(
            McpToolClient(stdio_target(log, "hang"), limits=slow).consult(query="q", tool=None)
            for log in uncapped
        ),
        return_exceptions=True,
    )
    c, d = (next(e["time"] for e in read_log(log) if e["event"] == "start") for log in uncapped)
    assert abs(c - d) < 0.7  # without the cap they run together


@pytest.mark.asyncio
async def test_only_the_first_hundred_tools_are_looked_at(tmp_path: Path) -> None:
    log = tmp_path / "stub.log"

    capped = await _failure(McpToolClient(stdio_target(log, "many_tools"), limits=_FAST))
    wide = McpLimits(max_tools=200, timeouts=ClientTimeouts(8.0, 8.0))
    found = await McpToolClient(stdio_target(log, "many_tools"), limits=wide).consult(
        query="charge", tool=None
    )

    assert capped is McpFailure.NO_USABLE_TOOL  # search_docs is tool 111, past the cap of 100
    assert found.texts == (FACT,)


@pytest.mark.asyncio
async def test_a_second_cancellation_during_teardown_still_kills_the_child(tmp_path: Path) -> None:
    """The overall deadline cancels a consultation with ``Task.cancel``, which an anyio shield does
    not stop; if it fires again while the child is being stopped, the child must still die."""
    log = tmp_path / "stub.log"
    limits = McpLimits(
        timeouts=ClientTimeouts(connect_seconds=30.0, call_seconds=30.0), kill_grace_seconds=3.0
    )
    task = asyncio.ensure_future(
        McpToolClient(stdio_target(log, "stubborn"), limits=limits).consult(
            query="charge", tool=None
        )
    )
    for _ in range(100):  # until the child has started
        await asyncio.sleep(0.1)
        if any(e["event"] == "start" for e in read_log(log)):
            break
    await asyncio.sleep(1.0)

    task.cancel()
    await asyncio.sleep(0.3)  # the polite stop is now waiting out its 3 s grace
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _gone(next(e for e in read_log(log) if e["event"] == "start")["pid"])


# --- diff-derived text only reaches a read-only tool, in a query-named parameter (ADR-0017 D8) ---


_Q: dict[str, object] = {"properties": {"query": {"type": "string"}}}


def test_a_tool_not_declared_read_only_needs_the_developer_to_have_named_it() -> None:
    tools = [_tool("search_docs", _Q, hints=None)]

    with pytest.raises(McpConsultError) as caught:
        choose_tool(tools, None)
    with pytest.raises(McpConsultError):
        choose_tool(tools, "search_docs")  # named by a repository file: not trusted
    assert caught.value.failure is McpFailure.NO_USABLE_TOOL
    assert choose_tool(tools, "search_docs", tool_trusted=True) == ("search_docs", "query")


@pytest.mark.parametrize(
    "hints",
    [
        types.ToolAnnotations(read_only_hint=False),
        types.ToolAnnotations(read_only_hint=True, destructive_hint=True),
        types.ToolAnnotations(read_only_hint=True, open_world_hint=True),
    ],
    ids=["not-read-only", "destructive", "open-world"],
)
def test_destructive_open_world_and_not_read_only_hints_refuse_automatic_choice(
    hints: types.ToolAnnotations,
) -> None:
    tools = [_tool("search_docs", _Q, hints=hints)]

    with pytest.raises(McpConsultError) as caught:
        choose_tool(tools, None)
    assert caught.value.failure is McpFailure.NO_USABLE_TOOL


@pytest.mark.parametrize("parameter", ["sql", "command", "expression", "path", "url", "script"])
@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("trusted", [True, False])
def test_the_query_never_goes_into_a_parameter_not_named_for_a_query(
    parameter: str, required: bool, trusted: bool
) -> None:
    schema: dict[str, object] = {"properties": {parameter: {"type": "string"}}}
    if required:
        schema["required"] = [parameter]
    tools = [_tool("search_records", schema)]

    for wanted in (None, "search_records"):
        with pytest.raises(McpConsultError) as caught:
            choose_tool(tools, wanted, tool_trusted=trusted)
        assert caught.value.failure is McpFailure.NO_USABLE_TOOL


@pytest.mark.parametrize("parameter", ["query", "q", "search", "text", "keywords", "prompt"])
def test_each_query_named_parameter_is_accepted(parameter: str) -> None:
    tools = [_tool("search_docs", {"properties": {parameter: {"type": "string"}}})]

    assert choose_tool(tools, None) == ("search_docs", parameter)


@pytest.mark.asyncio
async def test_a_server_tool_without_the_read_only_hint_is_skipped_unless_named_by_the_developer(
    tmp_path: Path,
) -> None:
    log = tmp_path / "stub.log"
    client = McpToolClient(stdio_target(log, "unannotated"), limits=_FAST)

    assert await _failure(client) is McpFailure.NO_USABLE_TOOL
    with pytest.raises(McpConsultError):
        await client.consult(query="charge", tool="search_docs")  # named by a repository file
    assert calls(log) == []

    named = await client.consult(query="charge", tool="search_docs", tool_trusted=True)

    assert named.texts == (FACT,)
    assert [c["tool"] for c in calls(log)] == ["search_docs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sql_param", "command_param"])
@pytest.mark.parametrize("tool", [None, "search_records"])
async def test_a_tool_with_a_sql_or_command_parameter_is_never_called(
    tmp_path: Path, mode: str, tool: str | None
) -> None:
    log = tmp_path / "stub.log"
    client = McpToolClient(stdio_target(log, mode), limits=_FAST)

    with pytest.raises(McpConsultError) as caught:
        await client.consult(query="payments/charge.py charge_card", tool=tool, tool_trusted=True)

    assert caught.value.failure is McpFailure.NO_USABLE_TOOL
    assert calls(log) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["destructive", "open_world"])
async def test_a_server_tool_declaring_a_destructive_or_open_world_hint_is_refused(
    tmp_path: Path, mode: str
) -> None:
    log = tmp_path / "stub.log"

    assert await _failure(McpToolClient(stdio_target(log, mode), limits=_FAST)) is (
        McpFailure.NO_USABLE_TOOL
    )
    assert calls(log) == []


@pytest.mark.asyncio
async def test_only_the_read_only_search_tool_is_called_when_a_write_tool_is_advertised(
    tmp_path: Path,
) -> None:
    log = tmp_path / "stub.log"

    result = await McpToolClient(stdio_target(log, "write_tool"), limits=_FAST).consult(
        query="charge", tool=None
    )

    assert result.texts == (FACT,)
    assert [c["tool"] for c in calls(log)] == ["search_docs"]  # update_page was never called


# --- a server that exits leaving children behind ----------------------------------------------


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"  # a zombie is dead; whoever adopted it reaps it


@pytest.mark.asyncio
async def test_children_a_server_left_behind_after_it_exited_are_killed(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = f"sleep 300 >/dev/null 2>&1 & echo $! > {pid_file}; exit 0"
    limits = McpLimits(timeouts=ClientTimeouts(connect_seconds=3.0, call_seconds=3.0))

    failure = await _failure(McpToolClient(StdioTarget("sh", ("-c", script)), limits=limits))

    assert failure is McpFailure.PROTOCOL_ERROR
    child = int(pid_file.read_text())
    for _ in range(60):
        if not _alive(child):
            break
        await asyncio.sleep(0.05)
    else:
        os.kill(child, 9)
    assert not _alive(child)


@pytest.mark.parametrize(
    ("returncode", "pid_in_use", "swept"),
    [(None, False, True), (0, False, True), (0, True, False)],
    ids=["leader-alive", "leader-gone-members-left", "leader-gone-pid-reused"],
)
def test_the_group_sweep_never_targets_a_recycled_pid(
    monkeypatch: pytest.MonkeyPatch, returncode: int | None, pid_in_use: bool, swept: bool
) -> None:
    from adapter.product_knowledge import mcp_tools

    killed: list[int] = []

    def kill(pid: int, sig: int) -> None:
        if sig == 0 and not pid_in_use:
            raise ProcessLookupError

    monkeypatch.setattr(mcp_tools.os, "kill", kill)
    monkeypatch.setattr(mcp_tools.os, "killpg", lambda pid, sig: killed.append(pid))
    process = type("P", (), {"pid": 4242, "returncode": returncode})()

    mcp_tools._kill_group(process)  # type: ignore[arg-type]

    assert killed == ([4242] if swept else [])


@pytest.mark.asyncio
async def test_a_stdio_server_is_refused_on_windows_where_supervision_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("adapter.product_knowledge.mcp_tools.sys.platform", "win32")
    client = McpToolClient(StdioTarget(str(tmp_path / "server")), limits=_FAST)

    assert await _failure(client) is McpFailure.START_FAILED
