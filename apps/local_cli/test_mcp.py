"""Integration tests for MCP knowledge: US-L9, US-L10, US-L11 and the MCP halves of US-L14 and
US-L15 (plan Phase 7, ADR-0017).

Everything is real except the model: real temporary git repositories, the CLI's own wiring, the real
shared context-builder provider branch, the real ``gitleaks`` binary for input redaction, and real
MCP servers built on the official SDK (``adapter/product_knowledge/testing/local_mcp_stub.py``)
spawned as child processes (stdio) or served on loopback (Streamable HTTP). The model is a recorded
stand-in that answers from what its prompt contains, so a test can tell whether a retrieved fact
reached it.

Needs ``git`` and ``gitleaks`` on PATH; each skips with a reason when absent.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import time
from pathlib import Path

import pytest
import respx
from pydantic import SecretStr

from adapter.llm.conftest import CapturedRequest, StartCapturingStub, canned_completion_json
from adapter.local.conftest import feature_branch_repo, requires_git
from adapter.product_knowledge.testing.local_mcp_harness import (
    calls,
    http_stub,
    read_log,
    stdio_target,
)
from adapter.product_knowledge.testing.local_mcp_stub import FACT, HOSTILE, SECRET_TOKEN
from apps.local_cli.main import EXIT_OK, EXIT_USAGE, run_review
from apps.local_cli.settings import LocalSettings
from apps.local_cli.test_ci import FakeGitHub
from review.diff_prompt import INERT_REFERENCE_DATA_FRAMING

pytestmark = [
    pytest.mark.integration,
    requires_git,
    pytest.mark.skipif(
        shutil.which("gitleaks") is None, reason="the gitleaks binary is not installed"
    ),
]

_PLANTED = "ghp_PLANTED_IN_CONFIG_AND_ENV_0123456789abcdef"
_FINDING_WITH_FACT = "total() must send an idempotency key"
_FINDING_WITHOUT = "total() has no obvious problem"


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("REVIEW_"):
            monkeypatch.delenv(name)


def _model(request: CapturedRequest) -> tuple[int, bytes]:
    """A recorded model that answers from its prompt: it knows the policy only if the prompt carries
    it, and then cites the context item that held it (``context_ref_ids``)."""
    system = request.json()["messages"][0]["content"]
    if "verification assistant" in system:
        content = json.dumps(
            {
                "verdicts": [
                    {
                        "index": 0,
                        "valid": True,
                        "actionable": True,
                        "specific": True,
                        "valuable": True,
                    }
                ]
            }
        )
    elif "summary (2-4 sentences)" in system:
        content = "Rewrites total()."
    else:
        prompt = request.prompt_text()
        cited = re.search(r'"id": (\d+), "title": "docs: ', prompt)
        if FACT in prompt and cited:
            finding: dict[str, object] = {
                "file": "app.py",
                "line": 2,
                "severity": "high",
                "message": _FINDING_WITH_FACT,
                "context_ref_ids": [int(cited.group(1))],
            }
        else:
            finding = {"file": "app.py", "line": 2, "severity": "low", "message": _FINDING_WITHOUT}
        content = json.dumps({"findings": [finding]})
    return 200, canned_completion_json(content=content)


class _Setup:
    """One test's repository, model, config file and stub servers."""

    def __init__(self, tmp_path: Path, endpoint: str, llm_requests: list[CapturedRequest]) -> None:
        self.tmp = tmp_path
        self.repo = feature_branch_repo(tmp_path / "repo")
        self.endpoint = endpoint
        self.llm_requests = llm_requests
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.config = tmp_path / "outside" / "mcp.json"  # outside the checkout, like a workflow's
        self.servers: dict[str, object] = {}

    def stdio(self, name: str, mode: str = "normal", **env: str) -> Path:
        """A stdio server entry running the stub; returns its log file."""
        log = self.tmp / f"{name}.log"
        target = stdio_target(log, mode, keyword="total")
        self.servers[name] = {
            "command": target.command,
            "args": list(target.args),
            "env": dict(target.env) | env,
        }
        return log

    def http(self, name: str, url: str, **headers: str) -> None:
        self.servers[name] = {"type": "http", "url": url, "headers": headers}

    def write_config(self) -> None:
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({"mcpServers": self.servers}))

    def settings(self, **extra: object) -> LocalSettings:
        self.write_config()
        return LocalSettings(
            llm_endpoint=self.endpoint,
            llm_model="local-model",
            static_analysis_enabled=False,
            mcp_config=self.config,
            **extra,  # type: ignore[arg-type]
        )

    async def run(
        self,
        settings: LocalSettings | None = None,
        *,
        mcp: tuple[str, ...] = (),
        dry_run: bool = False,
        output: Path | None = None,
        staged: bool = False,
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        status = await run_review(
            settings or self.settings(),
            base=None if staged else "main",
            staged=staged,
            repo_dir=self.repo.path,
            out=out,
            err=err,
            mcp=mcp,
            home=self.home,
            dry_run=dry_run,
            output=output,
        )
        return status, out.getvalue(), err.getvalue()

    def prompts(self) -> str:
        return "\n".join(r.prompt_text() for r in self.llm_requests)

    def everything_sent(self) -> str:
        return "\n".join(r.body.decode() for r in self.llm_requests)


@pytest.fixture
def setup(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> _Setup:
    return _Setup(tmp_path, stub_llm_capture(_model), llm_requests)


_SECRET_QUERY = "s3cr3t-in-a-url-query"


def _summary_line(err: str) -> str:
    return next(line for line in err.splitlines() if line.strip().startswith("mcp servers:"))


def _started(log: Path) -> bool:
    return any(e["event"] == "start" for e in read_log(log))


async def _gone(log: Path) -> bool:
    pid = next(e["pid"] for e in read_log(log) if e["event"] == "start")
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)  # the loop must run: it is what reaps the child
    return False


# --- US-L9: use the MCP servers I already have ---


@pytest.mark.asyncio
async def test_us_l9_no_server_is_started_or_contacted_unless_nominated(setup: _Setup) -> None:
    log = setup.stdio("docs")

    status, out, err = await setup.run()

    assert status == EXIT_OK
    assert not log.exists()  # the configured server was never started
    assert "mcp servers: none consulted" in err
    assert FACT not in setup.everything_sent()
    assert _FINDING_WITHOUT in out and "context from MCP server" not in out


@pytest.mark.asyncio
async def test_us_l9_a_nominated_server_is_consulted_and_the_output_names_it_per_finding(
    setup: _Setup, tmp_path: Path
) -> None:
    log = setup.stdio("docs")
    setup.stdio("unused")  # configured, not nominated
    json_out = tmp_path / "report.json"

    status, out, err = await setup.run(mcp=("docs",), output=json_out)

    assert status == EXIT_OK
    # The query is built from the diff, and the server was asked exactly once, by the tool it has.
    [call] = calls(log)
    assert call["tool"] == "search_docs" and call["query"] == "app.py total"
    assert not (tmp_path / "unused.log").exists()
    # Value test (a): the retrieved fact is in the prompt, framed as inert reference data, and the
    # request-sensitive model used it: the finding cites it, and the output says which server.
    prompt = setup.prompts()
    assert FACT in prompt and INERT_REFERENCE_DATA_FRAMING in prompt
    assert out.splitlines()[:2] == [
        f"app.py:2: high: {_FINDING_WITH_FACT}",
        "  context from MCP server docs",
    ]
    assert "used docs; 0" not in err and "used docs; context for app.py:2" in _summary_line(err)
    summary = json.loads(json_out.read_text())["summary"]["mcp_servers"]
    assert summary == [
        {
            "name": "docs",
            "status": "used",
            "reason": None,
            "truncated_items": 0,
            "supplied_findings": ["app.py:2"],
        }
    ]
    assert await _gone(log)


@pytest.mark.asyncio
async def test_us_l9_value_without_the_server_the_same_model_cannot_know_the_fact(
    setup: _Setup,
) -> None:
    setup.stdio("docs")

    _, out_without, _ = await setup.run()
    prompts_without = setup.prompts()
    setup.llm_requests.clear()
    _, out_with, _ = await setup.run(mcp=("docs",))

    assert FACT not in prompts_without and _FINDING_WITH_FACT not in out_without
    assert FACT in setup.prompts() and _FINDING_WITH_FACT in out_with


@pytest.mark.asyncio
async def test_us_l9_a_name_not_in_my_configuration_exits_non_zero_naming_it(
    setup: _Setup,
) -> None:
    log = setup.stdio("docs")

    status, out, err = await setup.run(mcp=("docs", "wiki"))

    assert status == EXIT_USAGE
    assert "'wiki'" in err and "not in your MCP configuration" in err
    assert out == "" and setup.llm_requests == []  # nothing was sent anywhere
    assert not log.exists()


@pytest.mark.asyncio
async def test_us_l9_the_server_is_found_in_the_developers_own_client_configuration(
    setup: _Setup,
) -> None:
    log = setup.stdio("docs")
    setup.write_config()
    (setup.home / ".claude.json").write_text(json.dumps({"mcpServers": setup.servers}))
    settings = LocalSettings(
        llm_endpoint=setup.endpoint, llm_model="local-model", static_analysis_enabled=False
    )

    status, _, err = await setup.run(settings, mcp=("docs",))

    assert status == EXIT_OK and "used docs" in err and calls(log)


@pytest.mark.asyncio
async def test_us_l9_a_committed_names_only_file_nominates_locally(setup: _Setup) -> None:
    log = setup.stdio("docs")
    setup.repo.write(".review/mcp.json", json.dumps({"mcp": {"servers": ["docs"]}}))
    setup.repo.commit("nominate docs")

    status, _, err = await setup.run()

    assert status == EXIT_OK and "used docs" in err and calls(log)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["command", "args", "env", "url", "headers"])
async def test_us_l9_a_repo_file_that_says_how_to_reach_a_server_is_rejected_and_nothing_runs(
    setup: _Setup, key: str
) -> None:
    log = setup.stdio("docs")
    value = f"touch {setup.tmp}/pwned"
    setup.repo.write(".review/mcp.json", json.dumps({"mcp": {"servers": ["docs"], key: value}}))
    setup.repo.commit("a hostile nomination file")

    status, _, err = await setup.run()

    assert status == EXIT_USAGE
    assert f"'{key}'" in err and ".review/mcp.json" in err
    assert value not in err and not log.exists() and not (setup.tmp / "pwned").exists()
    assert setup.llm_requests == []


@pytest.mark.asyncio
async def test_us_l9_a_server_only_the_repository_defines_and_nominates_is_refused(
    setup: _Setup,
) -> None:
    log = setup.stdio("docs")
    (setup.repo.path / ".mcp.json").write_text(json.dumps({"mcpServers": setup.servers}))
    setup.repo.write(".review/mcp.json", json.dumps({"mcp": {"servers": ["docs"]}}))
    setup.repo.commit("a repo that defines and nominates its own server")
    settings = LocalSettings(
        llm_endpoint=setup.endpoint, llm_model="local-model", static_analysis_enabled=False
    )

    status, _, err = await setup.run(settings)

    assert status == EXIT_USAGE and "nominate it yourself with --mcp" in err
    assert not log.exists()
    # The developer naming it themselves is their decision.
    status, _, _ = await setup.run(settings, mcp=("docs",))
    assert status == EXIT_OK and calls(log)


@pytest.mark.asyncio
async def test_us_l9_a_nomination_may_name_the_tool(setup: _Setup) -> None:
    log = setup.stdio("docs")

    status, _, err = await setup.run(mcp=("docs:search_docs",))
    assert (
        status == EXIT_OK
        and "used docs" in err
        and [c["tool"] for c in calls(log)] == ["search_docs"]
    )

    _, _, err = await setup.run(mcp=("docs:no_such_tool",))
    assert "errored docs (no usable tool)" in err


@pytest.mark.asyncio
async def test_us_l9_an_http_server_on_loopback_is_consulted_with_its_own_headers(
    setup: _Setup, tmp_path: Path
) -> None:
    with http_stub(tmp_path / "http.log", keyword="total") as stub:
        setup.http("docs", stub.url, **{"X-Api-Key": _PLANTED})

        status, out, err = await setup.run(mcp=("docs",))

        assert status == EXIT_OK and "used docs" in err
        assert "context from MCP server docs" in out
        requests = [e for e in read_log(stub.log) if e["event"] == "request"]
        assert requests and all(r["headers"]["x-api-key"] == _PLANTED for r in requests)
        assert _PLANTED not in out + err + setup.everything_sent()


# --- CI: nominations come from the base branch, configuration from the workflow ---


@pytest.mark.asyncio
async def test_us_l9_in_ci_a_nomination_added_by_the_pull_request_is_ignored(setup: _Setup) -> None:
    log = setup.stdio("docs")
    # `feature` is the pull-request head; it nominates the server, the base (`main`) does not.
    setup.repo.write(".review/mcp.json", json.dumps({"mcp": {"servers": ["docs"]}}))
    setup.repo.commit("the pull request nominates a server")
    settings = setup.settings(ci=True)

    status, out, _ = await setup.run(settings, dry_run=True)

    assert status == EXIT_OK
    assert "MCP servers: none consulted" in out
    assert not log.exists()
    assert setup.llm_requests == []


@pytest.mark.asyncio
async def test_us_l9_in_ci_a_nomination_on_the_base_branch_is_honoured(setup: _Setup) -> None:
    log = setup.stdio("docs")
    setup.repo.git("checkout", "-q", "main")
    setup.repo.write(".review/mcp.json", json.dumps({"mcp": {"servers": ["docs"]}}))
    setup.repo.commit("main nominates docs")
    setup.repo.git("checkout", "-q", "feature")
    setup.repo.git("merge", "-q", "--no-edit", "main")

    status, out, _ = await setup.run(setup.settings(ci=True), dry_run=True)

    assert status == EXIT_OK and "MCP server docs (stdio)" in out
    assert not log.exists()  # a dry run starts nothing


@pytest.mark.asyncio
async def test_us_l9_in_ci_stdio_is_refused_unless_the_workflow_provides_the_configuration(
    setup: _Setup,
) -> None:
    log = setup.stdio("docs")
    (setup.home / ".claude.json").write_text(json.dumps({"mcpServers": setup.servers}))
    (setup.repo.path / ".mcp.json").write_text(json.dumps({"mcpServers": setup.servers}))
    settings = LocalSettings(
        llm_endpoint=setup.endpoint, llm_model="local-model", static_analysis_enabled=False, ci=True
    )

    status, _, err = await setup.run(settings, mcp=("docs",), dry_run=True)

    assert status == EXIT_USAGE and "REVIEW_LOCAL_MCP_CONFIG" in err
    assert not log.exists()


@pytest.mark.asyncio
async def test_us_l9_in_ci_a_configuration_file_inside_the_checkout_is_refused(
    setup: _Setup,
) -> None:
    log = setup.stdio("docs")
    inside = setup.repo.path / "ci-mcp.json"
    inside.write_text(json.dumps({"mcpServers": setup.servers}))
    settings = LocalSettings(
        llm_endpoint=setup.endpoint,
        llm_model="local-model",
        static_analysis_enabled=False,
        ci=True,
        mcp_config=inside,
    )

    status, _, err = await setup.run(settings, mcp=("docs",), dry_run=True)

    assert status == EXIT_USAGE and "inside the checkout" in err
    assert not log.exists()


# --- the child's environment ---


@pytest.mark.asyncio
async def test_a_stdio_child_inherits_no_credential_and_runs_in_an_empty_directory(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", _PLANTED)
    monkeypatch.setenv("GH_TOKEN", _PLANTED)
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", _PLANTED)
    monkeypatch.setenv("OPENAI_API_KEY", _PLANTED)
    log = setup.stdio("docs", DOCS_KEY="configured-for-docs")

    status, _, _ = await setup.run(mcp=("docs",))

    start = next(e for e in read_log(log) if e["event"] == "start")
    assert status == EXIT_OK
    assert _PLANTED not in str(start["env"])
    assert start["env"]["DOCS_KEY"] == "configured-for-docs"
    assert Path(start["cwd"]) != setup.repo.path


# --- US-L10: a failing knowledge source never blocks the review ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "status_word", "reason"),
    [
        ("hang", "timed_out", "timeout"),
        ("crash", "errored", "protocol error"),
        ("error", "errored", "protocol error"),
        ("garbage", "errored", "not valid MCP output"),
        ("huge", "errored", "output over the size cap"),
        ("two_tools", "errored", "no usable tool"),
    ],
)
async def test_us_l10_a_failing_server_costs_the_context_not_the_review(
    setup: _Setup, mode: str, status_word: str, reason: str
) -> None:
    log = setup.stdio("docs", mode)
    settings = setup.settings(mcp_deadline_seconds=3.0)

    status, out, err = await setup.run(settings, mcp=("docs",))

    assert status == EXIT_OK
    assert f"{status_word} docs ({reason})" in _summary_line(err)
    assert out.splitlines()[0] == f"app.py:2: low: {_FINDING_WITHOUT}"  # the review completed
    assert FACT not in setup.everything_sent() and "context from MCP server" not in out
    assert await _gone(log)  # killed and reaped, not left running


@pytest.mark.asyncio
async def test_us_l10_the_deadline_is_total_across_servers(setup: _Setup) -> None:
    logs = [setup.stdio(f"slow{i}", "hang") for i in range(3)]
    settings = setup.settings(mcp_deadline_seconds=1.5)

    started = time.monotonic()
    status, _, err = await setup.run(settings, mcp=("slow0", "slow1", "slow2"))
    elapsed = time.monotonic() - started

    assert status == EXIT_OK
    assert _summary_line(err).count("timed_out") == 3
    assert elapsed < 12  # three hung servers cost one deadline, not three
    assert all([await _gone(log) for log in logs])


@pytest.mark.asyncio
async def test_us_l10_an_empty_result_completes_and_does_not_imply_context_was_used(
    setup: _Setup,
) -> None:
    setup.stdio("docs", "empty")

    status, out, err = await setup.run(mcp=("docs",))

    assert status == EXIT_OK
    assert "empty docs" in _summary_line(err) and "used" not in _summary_line(err)
    assert "context from MCP server" not in out
    assert "product knowledge" not in setup.prompts()  # no section at all


@pytest.mark.asyncio
async def test_us_l10_one_failing_server_does_not_stop_the_other(setup: _Setup) -> None:
    setup.stdio("bad", "crash")
    setup.stdio("docs")

    status, out, err = await setup.run(mcp=("bad", "docs"))

    line = _summary_line(err)
    assert status == EXIT_OK
    assert "errored bad (protocol error)" in line and "used docs" in line
    assert "context from MCP server docs" in out


@pytest.mark.asyncio
async def test_us_l10_an_http_server_error_is_reported_as_such(
    setup: _Setup, tmp_path: Path
) -> None:
    with http_stub(tmp_path / "http.log", "crash") as stub:
        setup.http("docs", stub.url)

        status, _, err = await setup.run(mcp=("docs",))

    assert status == EXIT_OK and "errored docs (protocol error)" in _summary_line(err)


# --- US-L11: content from a server is data, never instructions ---


@pytest.mark.asyncio
async def test_us_l11_hostile_content_is_framed_truncated_noted_and_triggers_nothing(
    setup: _Setup, tmp_path: Path
) -> None:
    log = setup.stdio("docs", "hostile")

    status, _, err = await setup.run(mcp=("docs",))

    prompt = setup.prompts()
    assert status == EXIT_OK
    # Only inside the framed reference section, cut at the 4,000-character content limit.
    section = prompt[prompt.index("MCP knowledge (docs)") :]
    assert INERT_REFERENCE_DATA_FRAMING in section.split("[", 1)[0]
    assert HOSTILE[:4000] in prompt and HOSTILE[:4001] not in prompt
    # The truncation is stated in the summary.
    assert "1 item(s) truncated to the size limits" in _summary_line(err)
    # No further tool call and no command: the one search call is the only call there was.
    assert [c["tool"] for c in calls(log)] == ["search_docs"]
    assert not (tmp_path / "pwned").exists()


# --- US-L15 (MCP half): nothing re-enters a prompt or leaves in a query unscreened ---


@pytest.mark.asyncio
async def test_us_l15_a_secret_in_a_server_response_never_reaches_the_prompt(
    setup: _Setup, tmp_path: Path
) -> None:
    setup.stdio("docs", "secret")
    json_out, md_out = tmp_path / "r.json", tmp_path / "r.md"

    status, out, err = await setup.run(mcp=("docs",), output=json_out)
    await setup.run(mcp=("docs",), output=md_out)

    everything = setup.everything_sent() + out + err + json_out.read_text() + md_out.read_text()
    assert status == EXIT_OK
    assert SECRET_TOKEN not in everything
    assert "[REDACTED]" in setup.prompts() and FACT in setup.prompts()


@pytest.mark.asyncio
async def test_us_l15_a_secret_in_the_diff_is_absent_from_the_query_a_server_receives(
    setup: _Setup,
) -> None:
    log = setup.stdio("docs")
    setup.repo.write(
        "app.py", f"def total(items):\n    return sum(items)\n\ndef {SECRET_TOKEN}():\n    pass\n"
    )
    setup.repo.commit("a secret in a symbol name")

    await setup.run(mcp=("docs",))

    assert SECRET_TOKEN not in json.dumps(read_log(log)) and calls(log)


@pytest.mark.asyncio
async def test_us_l15_a_scanner_that_fails_sends_the_server_nothing(
    setup: _Setup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The diff scan runs first and fails closed, so no query is built and no server is started."""
    log = setup.stdio("docs")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "gitleaks").write_text("#!/bin/sh\nexit 2\n")
    (fake / "gitleaks").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")

    status, _, err = await setup.run(mcp=("docs",))

    assert status != EXIT_OK and "redaction failed, nothing sent" in err
    assert not log.exists() and setup.llm_requests == []


# --- US-L14 (MCP half): the dry run lists each server and its query ---


@pytest.mark.asyncio
async def test_us_l14_a_dry_run_lists_each_server_and_the_exact_query_and_contacts_none(
    setup: _Setup, tmp_path: Path
) -> None:
    logs = [setup.stdio("docs"), setup.stdio("wiki")]
    with http_stub(tmp_path / "http.log") as stub:
        setup.http("kb", stub.url)

        status, out, err = await setup.run(mcp=("docs:search_docs", "wiki", "kb"), dry_run=True)

        assert status == EXIT_OK
        assert not any(log.exists() for log in logs)
        assert not [e for e in read_log(stub.log) if e["event"] == "request"]
    assert "MCP server docs (stdio): tool search_docs; query 'app.py total'" in out
    assert (
        "MCP server wiki (stdio): the tool is chosen from the server's tool list at run time; "
        "query 'app.py total'" in out
    )
    assert "MCP server kb (http, 127.0.0.1)" in out and "MCP servers: none" not in out
    assert "not_consulted docs (dry run)" in _summary_line(err)


@pytest.mark.asyncio
async def test_us_l14_a_dry_run_never_shows_a_configured_value(
    setup: _Setup, tmp_path: Path
) -> None:
    setup.stdio("docs", DOCS_KEY=_PLANTED)
    setup.http(
        "kb", f"http://u:{_PLANTED}@127.0.0.1:9/mcp?token={_PLANTED}", Authorization=_PLANTED
    )

    status, out, err = await setup.run(mcp=("docs", "kb"), dry_run=True)

    assert status == EXIT_OK and _PLANTED not in out + err
    assert "MCP server kb (http, 127.0.0.1)" in out


# --- a planted token in MCP config or environment never appears in any output ---


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "crash", "hostile", "garbage"])
async def test_a_planted_token_in_mcp_config_and_environment_is_absent_from_every_output(
    setup: _Setup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", _PLANTED)
    monkeypatch.setenv("OPENAI_API_KEY", _PLANTED)
    setup.stdio("docs", mode, DOCS_API_KEY=_PLANTED)
    json_out, md_out = tmp_path / "r.json", tmp_path / "r.md"
    settings = setup.settings(mcp_deadline_seconds=3.0)

    status, out, err = await setup.run(settings, mcp=("docs",), output=json_out)
    await setup.run(settings, mcp=("docs",), output=md_out)
    _, dry_out, dry_err = await setup.run(settings, mcp=("docs",), dry_run=True)

    everything = "".join(
        [
            out,
            err,
            dry_out,
            dry_err,
            json_out.read_text(),
            md_out.read_text(),
            setup.everything_sent(),
        ]
    )
    assert status == EXIT_OK
    assert _PLANTED not in everything


@pytest.mark.asyncio
async def test_a_planted_token_is_absent_from_the_error_for_an_unusable_entry(
    setup: _Setup,
) -> None:
    setup.servers["docs"] = {
        "command": ["not", "a", "string"],
        "env": {"K": _PLANTED},
    }

    status, out, err = await setup.run(mcp=("docs",))

    assert status == EXIT_USAGE and "'docs'" in err
    assert _PLANTED not in out + err


@pytest.mark.asyncio
async def test_a_planted_token_is_absent_from_logs_at_debug_level(
    setup: _Setup, caplog: pytest.LogCaptureFixture
) -> None:
    setup.stdio("docs", DOCS_API_KEY=_PLANTED)
    caplog.set_level("DEBUG")

    await setup.run(mcp=("docs",))

    assert _PLANTED not in caplog.text


# --- a real CI run: the pull-request review shows which server backed a finding ---


@pytest.mark.asyncio
async def test_us_l9_in_a_real_ci_run_the_workflow_defined_server_backs_a_finding_in_the_comment(
    setup: _Setup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", _PLANTED)
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", _PLANTED)
    log = setup.stdio("docs", DOCS_KEY="configured-for-docs")
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {"action": "synchronize", "pull_request": {"number": 7, "head": {"sha": "a" * 40}}}
        )
    )
    settings = setup.settings(
        github_actions=True,
        github_event_name="pull_request",
        github_token=SecretStr(_PLANTED),
        github_repository="acme/widgets",
        github_event_path=event,
        github_max_retry_attempts=0,
    )

    with respx.mock(assert_all_called=False, assert_all_mocked=False) as router:
        router.route(host="127.0.0.1").pass_through()
        github = FakeGitHub(router)
        status, out, err = await setup.run(settings, mcp=("docs",))
        review = github.posted_review()

    start = next(e for e in read_log(log) if e["event"] == "start")
    assert status == EXIT_OK
    assert _PLANTED not in str(start["env"]) and start["env"]["DOCS_KEY"] == "configured-for-docs"
    [comment] = review["comments"]
    # (The comment is markdown-escaped, so brackets and underscores carry a backslash.)
    assert "total\\(\\) must send an idempotency key" in comment["body"]
    assert "Product knowledge (MCP knowledge \\(docs\\))" in comment["body"]
    assert "docs: search\\_docs result 1" in comment["body"]
    assert FACT not in json.dumps(review)  # the retrieved text itself is never posted
    assert _PLANTED not in json.dumps(review) + out + err
    assert "used docs; context for app.py:2" in _summary_line(err)


# --- diff-derived text only reaches a read-only tool, in a query-named parameter (ADR-0017 D8) ---


@pytest.mark.asyncio
async def test_us_l9_a_tool_without_the_read_only_hint_is_skipped_unless_i_name_it(
    setup: _Setup,
) -> None:
    log = setup.stdio("docs", "unannotated")

    status, _, err = await setup.run(mcp=("docs",))
    assert status == EXIT_OK and "errored docs (no usable tool)" in err and not calls(log)

    # A repository file naming the tool does not make it trusted either.
    setup.repo.write(
        ".review/mcp.json",
        json.dumps({"mcp": {"servers": [{"name": "docs", "tool": "search_docs"}]}}),
    )
    setup.repo.commit("a file names the tool")
    status, _, err = await setup.run()
    assert status == EXIT_OK and "errored docs (no usable tool)" in err and not calls(log)

    # The developer naming it on the command line is their own decision.
    status, _, err = await setup.run(mcp=("docs:search_docs",))
    assert (
        status == EXIT_OK
        and "used docs" in err
        and [c["tool"] for c in calls(log)] == ["search_docs"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sql_param", "command_param"])
async def test_us_l9_diff_text_is_never_sent_to_a_tool_that_takes_sql_or_a_command(
    setup: _Setup, mode: str
) -> None:
    log = setup.stdio("docs", mode)

    for option in ("docs", "docs:search_records"):
        status, _, err = await setup.run(mcp=(option,))

        assert status == EXIT_OK and "errored docs (no usable tool)" in err
    assert calls(log) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["destructive", "open_world"])
async def test_us_l9_a_destructive_or_open_world_tool_is_never_called_automatically(
    setup: _Setup, mode: str
) -> None:
    log = setup.stdio("docs", mode)

    status, _, err = await setup.run(mcp=("docs",))

    assert status == EXIT_OK and "errored docs (no usable tool)" in err and not calls(log)


@pytest.mark.asyncio
async def test_us_l9_a_write_tool_the_server_also_advertises_is_never_called(
    setup: _Setup,
) -> None:
    log = setup.stdio("docs", "write_tool")

    status, _, err = await setup.run(mcp=("docs",))

    assert status == EXIT_OK and "used docs" in err
    assert {c["tool"] for c in calls(log)} == {"search_docs"}


# --- hostile or broken configuration is a usage error, never a traceback ---


@pytest.mark.asyncio
async def test_us_l9_a_deeply_nested_nomination_file_exits_2_not_with_a_traceback(
    setup: _Setup,
) -> None:
    log = setup.stdio("docs")
    depth = 3000
    setup.repo.write(".review/mcp.json", '{"mcp": ' + "[" * depth + "]" * depth + "}")
    setup.repo.commit("a hostile, deeply nested nomination file")

    status, _, err = await setup.run()

    assert status == EXIT_USAGE and ".review/mcp.json" in err and "Traceback" not in err
    assert not log.exists() and setup.llm_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://[::1/mcp", "http://127.0.0.1:notaport/mcp"])
async def test_us_l9_a_malformed_server_url_exits_2_without_echoing_it(
    setup: _Setup, url: str
) -> None:
    setup.http("web", url + "?token=" + _SECRET_QUERY)

    status, _, err = await setup.run(mcp=("web",))

    assert status == EXIT_USAGE and "'web'" in err and "Traceback" not in err
    assert _SECRET_QUERY not in err and setup.llm_requests == []


@pytest.mark.asyncio
async def test_us_l9_in_ci_with_staged_the_head_is_not_a_trusted_source_for_nominations(
    setup: _Setup,
) -> None:
    log = setup.stdio("docs")
    # With --staged the "base commit" is HEAD: the pull request's own commit.
    setup.repo.write(".review/mcp.json", json.dumps({"mcp": {"servers": ["docs"]}}))
    setup.repo.commit("the pull request nominates a server")

    status, _, err = await setup.run(setup.settings(ci=True), staged=True, dry_run=True)

    assert status == EXIT_USAGE and "--staged" in err and ".review/mcp.json" in err
    assert not log.exists() and setup.llm_requests == []
