"""Unit tests for the conformance tool's runner, CLI and byte counter, with the
check list and the MCP session replaced by fakes. The checks themselves are
proven against a real local server in ``test_conformance.py``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from adapter.product_knowledge import conformance as conf
from adapter.product_knowledge.conformance import CheckResult, ConformanceTarget, Status


class _Refuses:
    """An async context manager whose ``__aenter__`` raises ``error``."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __aenter__(self) -> Any:
        raise self.error

    async def __aexit__(self, *_: object) -> None:
        return None


def test_describe_unwraps_exception_groups_and_reports_the_http_status() -> None:
    class _Http(Exception):
        response = type("R", (), {"status_code": 401})()

    assert (
        conf._describe(ExceptionGroup("g", [ExceptionGroup("n", [_Http()])])) == "_Http (HTTP 401)"
    )
    assert conf._describe(ValueError("secret provider bytes")) == "ValueError"


@pytest.mark.asyncio
async def test_run_checks_maps_each_outcome_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def passes(run: Any) -> str:
        return "ok"

    async def fails(run: Any) -> str:
        raise conf._Failed("bad shape at $.results[0]")

    async def skips(run: Any) -> str:
        raise conf._Skipped("not applicable")

    async def explodes(run: Any) -> str:
        raise RuntimeError("provider said something sensitive")

    monkeypatch.setattr(
        conf,
        "_CHECKS",
        (("a", passes), ("b", fails), ("c", skips), ("d", explodes), ("e", passes)),
    )

    results = await conf._run_checks(object())  # type: ignore[arg-type]

    assert results == [
        CheckResult("a", Status.PASS, "ok"),
        CheckResult("b", Status.FAIL, "bad shape at $.results[0]"),
        CheckResult("c", Status.SKIP, "not applicable"),
        CheckResult("d", Status.FAIL, "check raised RuntimeError"),  # message never echoed
        CheckResult("e", Status.PASS, "ok"),
    ]


@pytest.mark.asyncio
async def test_auth_check_is_skipped_without_a_token() -> None:
    result = await conf._auth_rejects_bad_credentials(ConformanceTarget(url="http://x/mcp"))

    assert result.status is Status.SKIP and "PK_TOKEN" in result.detail


@pytest.mark.parametrize(
    ("accepted", "status", "detail"),
    [
        pytest.param([False, False], Status.PASS, "rejected", id="both-rejected"),
        pytest.param([True], Status.FAIL, "no token was accepted", id="anonymous-accepted"),
        pytest.param([False, True], Status.FAIL, "invalid token was accepted", id="bad-accepted"),
    ],
)
@pytest.mark.asyncio
async def test_auth_check_requires_both_probes_to_be_rejected(
    monkeypatch: pytest.MonkeyPatch, accepted: list[bool], status: Status, detail: str
) -> None:
    answers = iter(accepted)

    async def fake_accepted(target: ConformanceTarget, token: str | None) -> bool:
        return next(answers)

    monkeypatch.setattr(conf, "_accepted", fake_accepted)
    target = ConformanceTarget(url="http://x/mcp", token=SecretStr("t"))

    result = await conf._auth_rejects_bad_credentials(target)

    assert result.status is status and detail in result.detail


@pytest.mark.asyncio
async def test_accepted_is_false_when_the_session_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(*_: Any) -> _Refuses:
        return _Refuses(ConnectionError("401"))

    monkeypatch.setattr(conf, "_open", refused)

    assert await conf._accepted(ConformanceTarget(url="http://x/mcp"), None) is False


@pytest.mark.asyncio
async def test_run_conformance_reports_an_unreachable_server_as_a_failed_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(*_: Any) -> _Refuses:
        return _Refuses(ConnectionError("refused"))

    monkeypatch.setattr(conf, "_open", unreachable)

    results = await conf.run_conformance(ConformanceTarget(url="http://x/mcp"))

    assert [(r.name, r.status) for r in results] == [("connect", Status.FAIL)]
    assert "ConnectionError" in results[0].detail


def _patch_run(
    monkeypatch: pytest.MonkeyPatch, results: list[CheckResult]
) -> list[ConformanceTarget]:
    seen: list[ConformanceTarget] = []

    async def fake_run(target: ConformanceTarget) -> list[CheckResult]:
        seen.append(target)
        return results

    monkeypatch.setattr(conf, "run_conformance", fake_run)
    return seen


def test_main_exits_zero_when_every_check_passes_or_skips(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _patch_run(
        monkeypatch,
        [CheckResult("a", Status.PASS, "fine"), CheckResult("b", Status.SKIP, "n/a")],
    )
    monkeypatch.setenv("PK_TOKEN", "  tok  ")

    code = conf.main(["--pk-url", "http://kb/mcp", "--pk-query", "pricing"])

    out = capsys.readouterr().out
    assert code == 0
    assert "PASS a: fine" in out and "SKIP b: n/a" in out and "2 of 2 checks passed" in out
    assert seen[0].url == "http://kb/mcp" and seen[0].query == "pricing"
    assert seen[0].token is not None and seen[0].token.get_secret_value() == "tok"
    assert "tok" not in out


def test_main_exits_one_on_a_failed_check_and_passes_no_token_when_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _patch_run(monkeypatch, [CheckResult("a", Status.FAIL, "broken")])
    monkeypatch.delenv("PK_TOKEN", raising=False)

    code = conf.main(["--pk-url", "http://kb/mcp"])

    assert code == 1 and seen[0].token is None
    assert "FAIL a: broken" in capsys.readouterr().out


def test_main_requires_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PK_URL", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        conf.main([])

    assert exit_info.value.code == 2


@pytest.mark.asyncio
async def test_counting_stream_tracks_the_largest_single_response_body() -> None:
    class _Inner(httpx2.AsyncByteStream):
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in self.chunks:
                yield chunk

        async def aclose(self) -> None:
            self.closed = True

    transport = conf._CountingTransport()
    first, second = _Inner([b"abc", b"de"]), _Inner([b"x"])

    body = b"".join([c async for c in conf._CountingStream(first, transport)])
    _ = [c async for c in conf._CountingStream(second, transport)]
    await conf._CountingStream(first, transport).aclose()

    assert body == b"abcde" and transport.largest == 5  # per response, not cumulative
    assert first.closed is True
