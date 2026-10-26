"""Integration tests for how a local run treats credentials and CI signals (audit fixes to
docs/plans/plan-local-mode.md, Phases 4, 5 and 8):

- the runtime-failure line masks credentials in the endpoint, query string included;
- no credential-named variable reaches a git, gitleaks or analyzer child process;
- a planted model API key never appears in any output, report file or dry-run preview;
- a generic CI signal (``CI=true``) without a recognised pull-request context refuses to read the
  memory file from the working tree;
- post-redaction text is what memory suppression keys on and what is printed;
- a normal run contacts only the configured model host.

Real git repositories, the real registry, the recorded model on a local stub, real ``gitleaks``.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import socket
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import SecretStr

from adapter.analyzers.subprocess_runner import analyzer_env, run_analyzer_subprocess
from adapter.llm.conftest import (
    CapturedRequest,
    StartCapturingStub,
    canned_completion_json,
    unused_port_endpoint,
)
from adapter.local.conftest import Repo, feature_branch_repo, init_repo, requires_git
from adapter.local.environment import is_credential_name, scrubbed_environment
from adapter.local.git import run_git
from apps.local_cli.main import EXIT_OK, EXIT_RUNTIME_FAILURE, EXIT_USAGE, run_review
from apps.local_cli.settings import LocalSettings
from apps.local_cli.test_local_review import _model_reply

pytestmark = [
    pytest.mark.integration,
    requires_git,
    pytest.mark.skipif(
        shutil.which("gitleaks") is None, reason="the gitleaks binary is not installed"
    ),
]

_PLANTED = {
    "GITHUB_TOKEN": "ghs_planted_github_token_0001",
    "GH_TOKEN": "ghp_planted_gh_token_0002",
    "ACTIONS_RUNTIME_TOKEN": "planted_runtime_token_0003",
    "REVIEW_LOCAL_LLM_API_KEY": "planted_model_key_0004",
    "AWS_SECRET_ACCESS_KEY": "planted_aws_secret_0005",
    "CUSTOM_SERVICE_TOKEN": "planted_custom_token_0006",
}
_API_KEY = "sk-CANARYmodelkey0123456789abcdef"


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("REVIEW_"):
            monkeypatch.delenv(name)


def _settings(endpoint: str, **extra: object) -> LocalSettings:
    values: dict[str, object] = {
        "llm_endpoint": endpoint,
        "llm_model": "local-model",
        "static_analysis_enabled": False,
        **extra,
    }
    return LocalSettings(**values)  # type: ignore[arg-type]


async def _run(
    repo: Repo,
    settings: LocalSettings,
    *,
    dry_run: bool = False,
    output: Path | None = None,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = await run_review(
        settings,
        base="main",
        repo_dir=repo.path,
        out=out,
        err=err,
        dry_run=dry_run,
        output=output,
    )
    return status, out.getvalue(), err.getvalue()


# --- the failure line masks the endpoint ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_runtime_failure_line_masks_userinfo_and_query_credentials(
    tmp_path: Path,
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    port_url = unused_port_endpoint()
    endpoint = port_url.replace("http://", "http://user:hunter2@") + "?api-key=SECRET"

    status, out, err = await _run(repo, _settings(endpoint))

    assert status == EXIT_RUNTIME_FAILURE
    assert "127.0.0.1" in err  # the endpoint is still named (US-L6)
    for text in (out, err):
        assert "SECRET" not in text and "hunter2" not in text


# --- credentials never reach a child process ------------------------------------------------------


def test_credential_names_are_recognised_and_ordinary_ones_are_not() -> None:
    for name in (*_PLANTED, "gh_token", "NPM_TOKEN", "DB_PASSWORD", "OPENAI_API_KEY"):
        assert is_credential_name(name), name
    for name in ("PATH", "HOME", "LANG", "GITHUB_REPOSITORY", "GITHUB_EVENT_NAME", "CI"):
        assert not is_credential_name(name), name


@pytest.mark.asyncio
async def test_git_and_analyzer_children_do_not_inherit_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "repo")
    for name, value in _PLANTED.items():
        monkeypatch.setenv(name, value)

    async def child_environments(environ: Mapping[str, str]) -> str:
        _, git_env = await run_git(repo.path, "-c", "alias.dump=!env", "dump", environ=environ)
        analyzer_out = await run_analyzer_subprocess(
            ["env"], cwd=repo.path, timeout=10, env=analyzer_env(environ)
        )
        return "\n".join([git_env.decode(), analyzer_out.decode()])

    # Premise: handed the raw environment, the helpers leak everything but REVIEW_* (so the check
    # below would fail without the scrub).
    unscrubbed = await child_environments(os.environ)
    assert _PLANTED["GITHUB_TOKEN"] in unscrubbed
    assert _PLANTED["GH_TOKEN"] in unscrubbed

    scrubbed_env = scrubbed_environment(os.environ)
    scrubbed = await child_environments(scrubbed_env)

    for name, value in _PLANTED.items():
        assert name not in scrubbed and value not in scrubbed, name
    assert "PATH=" in scrubbed  # ordinary variables survive
    assert os.environ["GITHUB_TOKEN"] == _PLANTED["GITHUB_TOKEN"]  # the process is untouched


@pytest.mark.asyncio
async def test_a_review_run_gives_its_analyzer_subprocesses_no_credentials(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = tmp_path / "ruff-saw-this-environment"
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "ruff"
    shim.write_text(f"#!/bin/sh\nenv > '{seen}'\necho '[]'\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    for name, value in _PLANTED.items():
        monkeypatch.setenv(name, value)
    repo = feature_branch_repo(tmp_path / "repo")
    settings = _settings(stub_llm_capture(_model_reply), static_analysis_enabled=True)

    status, _, err = await _run(repo, settings)

    assert status == EXIT_OK, err
    assert seen.exists(), "the analyzer subprocess never ran, so the test proves nothing"
    child_environment = seen.read_text()
    for name, value in _PLANTED.items():
        assert name not in child_environment and value not in child_environment, name
    assert os.environ["GITHUB_TOKEN"] == _PLANTED["GITHUB_TOKEN"]  # restored after the run


# --- the model API key never appears anywhere -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_planted_model_api_key_is_absent_from_every_output_file_and_log(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("REVIEW_LOCAL_LLM_API_KEY", _API_KEY)
    repo = feature_branch_repo(tmp_path / "repo")
    endpoint = stub_llm_capture(_model_reply)
    json_target, md_target = tmp_path / "r.json", tmp_path / "r.md"
    settings = LocalSettings(
        llm_endpoint=endpoint,
        llm_model="local-model",
        static_analysis_enabled=False,
    )
    assert settings.llm_api_key.get_secret_value() == _API_KEY  # read from the environment

    real_json = await _run(repo, settings, output=json_target)
    real_md = await _run(repo, settings, output=md_target)
    dry = await _run(repo, settings, dry_run=True)
    failed = await _run(
        repo,
        LocalSettings(
            llm_endpoint=unused_port_endpoint(),
            llm_model="local-model",
            static_analysis_enabled=False,
        ),
    )

    assert [real_json[0], real_md[0], dry[0], failed[0]] == [
        EXIT_OK,
        EXIT_OK,
        EXIT_OK,
        EXIT_RUNTIME_FAILURE,
    ]
    everything = [*real_json[1:], *real_md[1:], *dry[1:], *failed[1:]]
    everything += [json_target.read_text(), md_target.read_text(), caplog.text, repr(settings)]
    for text in everything:
        assert _API_KEY not in text


# --- generic CI without a recognised pull-request context -----------------------------------------


def _repo_with_memory_in_the_working_tree(path: Path) -> Repo:
    repo = feature_branch_repo(path)
    entry = {
        "file": "app.py",
        "rule_id": None,
        "message": "result starts as an int, so a list of floats mixes types",
    }
    repo.write(".review/memory.jsonl", json.dumps(entry) + "\n")  # untracked, i.e. the PR head's
    return repo


@pytest.mark.asyncio
async def test_a_generic_ci_run_refuses_to_read_the_memory_file_from_the_working_tree(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = _repo_with_memory_in_the_working_tree(tmp_path / "repo")

    status, out, err = await _run(repo, _settings(stub_llm_capture(_model_reply), ci=True))

    assert status == EXIT_USAGE
    assert "CI was detected" in err and "REVIEW_LOCAL_MEMORY_FROM_WORKING_TREE" in err
    assert out == "" and llm_requests == []  # refused before anything was sent


@pytest.mark.asyncio
async def test_a_generic_ci_run_proceeds_when_the_memory_file_is_disabled_or_explicitly_trusted(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = _repo_with_memory_in_the_working_tree(tmp_path / "repo")
    endpoint = stub_llm_capture(_model_reply)

    disabled, out, _ = await _run(repo, _settings(endpoint, ci=True, memory_file_enabled=False))
    assert disabled == EXIT_OK
    assert "app.py:2: high:" in out  # not suppressed: the file was not read

    trusted, out, err = await _run(
        repo, _settings(endpoint, ci=True, memory_from_working_tree=True)
    )
    assert trusted == EXIT_OK
    assert "app.py:2: high:" not in out.splitlines()  # the explicit opt-in read it
    assert "memory file: loaded with 1 entries" in err


@pytest.mark.asyncio
async def test_outside_ci_the_memory_file_is_read_from_the_working_tree_as_before(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = _repo_with_memory_in_the_working_tree(tmp_path / "repo")

    status, out, _ = await _run(repo, _settings(stub_llm_capture(_model_reply)))

    assert status == EXIT_OK
    assert "app.py:2: high:" not in out.splitlines()


# --- post-redaction text is what memory keys on and what is printed -------------------------------

_SECRET = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"


def _leaky_model(request: CapturedRequest) -> tuple[int, bytes]:
    system = request.json()["messages"][0]["content"]
    if "verification assistant" in system:
        verdict = {"valid": True, "actionable": True, "specific": True, "valuable": True}
        content = json.dumps({"verdicts": [{"index": 0, **verdict}]})
    elif "summary (2-4 sentences)" in system:
        content = "Rewrites total()."
    else:
        message = f"the accumulator is seeded with the token {_SECRET} and never reset"
        content = json.dumps(
            {"findings": [{"file": "app.py", "line": 2, "severity": "high", "message": message}]}
        )
    return 200, canned_completion_json(content=content)


@pytest.mark.asyncio
async def test_memory_suppression_keys_and_printed_items_use_the_redacted_text(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    settings = _settings(stub_llm_capture(_leaky_model))

    status, out, err = await _run(repo, settings)  # the first run prints the redacted finding

    assert status == EXIT_OK, err
    [printed] = [line for line in out.splitlines() if line.startswith("app.py:2: high: ")]
    redacted_message = printed.removeprefix("app.py:2: high: ")
    assert _SECRET not in out + err and "[REDACTED" in redacted_message
    # The developer dismisses it by copying exactly what review printed.
    repo.write(
        ".review/memory.jsonl",
        json.dumps({"file": "app.py", "rule_id": None, "message": redacted_message}) + "\n",
    )

    status, out, err = await _run(repo, settings)

    assert status == EXIT_OK, err
    assert "app.py:2: high:" not in out.splitlines()  # matched on the redacted text
    assert "Suppressed by .review/memory.jsonl (1):" in out
    assert f"  app.py: {redacted_message}" in out  # the printed item is the redacted text
    assert _SECRET not in out + err


# --- a normal run phones nobody but the configured model host -------------------------------------


@pytest.fixture
def contacted(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    seen: list[object] = []
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def connect(self: socket.socket, address: object) -> None:
        seen.append(address)
        real_connect(self, address)  # type: ignore[arg-type]

    def getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        seen.append(host)
        return real_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return seen


@pytest.mark.asyncio
async def test_a_normal_run_with_telemetry_configured_contacts_only_the_model_host(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    contacted: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An exporter endpoint in the environment must not be dialled: there is no telemetry in local
    # mode. (In-process sockets only: the analyzer children are not observable from here.)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://telemetry.invalid:4318")
    monkeypatch.setenv("LANGFUSE_HOST", "http://telemetry.invalid:3000")
    repo = feature_branch_repo(tmp_path / "repo")
    endpoint = stub_llm_capture(_model_reply)

    status, _, err = await _run(repo, _settings(endpoint, static_analysis_enabled=True))

    assert status == EXIT_OK, err
    stub_port = int(endpoint.split(":")[2].split("/")[0])
    addresses = [a for a in contacted if isinstance(a, tuple)]
    assert addresses, "the capture saw no connection, so it proves nothing"
    assert {(a[0], a[1]) for a in addresses} == {("127.0.0.1", stub_port)}
    assert {h for h in contacted if isinstance(h, str)} <= {"127.0.0.1"}


def test_the_secret_str_api_key_is_masked_in_the_settings_repr() -> None:
    assert _API_KEY not in repr(LocalSettings(llm_api_key=SecretStr(_API_KEY)))
