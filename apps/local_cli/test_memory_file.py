"""Integration tests for the repository memory file (docs/plans/plan-local-mode.md, Phase 8):
US-L12 (dismiss a finding once) and US-L13 (learned rules arrive as a diff I can review).

The whole local pipeline runs for real, as in ``test_local_review.py``: a real temporary git
repository, the recorded model on a local stub endpoint, real ``gitleaks``. The CI tests add a
``respx`` cassette for GitHub. Needs ``git`` and ``gitleaks`` on PATH.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from adapter.llm.conftest import (
    CapturedRequest,
    RequestResponder,
    StartCapturingStub,
    canned_completion_json,
)
from adapter.local.conftest import (
    BASE_APP,
    FEATURE_APP,
    Repo,
    feature_branch_repo,
    init_repo,
    requires_git,
)
from adapter.local.memory_file import DEFAULT_MEMORY_FILE
from apps.local_cli.main import EXIT_OK, EXIT_USAGE, run_review
from apps.local_cli.settings import LocalSettings

pytestmark = [
    pytest.mark.integration,
    requires_git,
    pytest.mark.skipif(
        shutil.which("gitleaks") is None, reason="the gitleaks binary is not installed"
    ),
]

_MESSAGE = "result starts as an int, so a list of floats mixes types"
_OTHER_MESSAGE = "the loop adds without a type check"
_FINDING_2 = {"file": "app.py", "line": 2, "severity": "high", "message": _MESSAGE}
_FINDING_4 = {"file": "app.py", "line": 4, "severity": "high", "message": _OTHER_MESSAGE}
API = "https://api.github.com"
TOKEN = "ghs_CANARYtoken0123456789abcdefghijklmnopqrstuv"


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("REVIEW_"):
            monkeypatch.delenv(name)


def _model(findings: list[dict[str, Any]]) -> RequestResponder:
    """The recorded model: reports exactly ``findings`` and verifies every one of them."""

    def respond(request: CapturedRequest) -> tuple[int, bytes]:
        system = request.json()["messages"][0]["content"]
        if "verification assistant" in system:
            verdict = {"valid": True, "actionable": True, "specific": True, "valuable": True}
            content = json.dumps(
                {"verdicts": [{"index": i, **verdict} for i in range(len(findings))]}
            )
        elif "summary (2-4 sentences)" in system:
            content = "Rewrites total() as an explicit accumulation loop."
        else:
            content = json.dumps({"findings": findings})
        return 200, canned_completion_json(content=content)

    return respond


def _entry(file: str = "app.py", message: str = _MESSAGE, **extra: Any) -> str:
    return json.dumps({"file": file, "rule_id": None, "message": message, **extra})


def _write_memory(repo: Repo, *lines: str, path: str = DEFAULT_MEMORY_FILE) -> None:
    # Left untracked on purpose: an uncommitted file is not part of the reviewed diff.
    repo.write(path, "\n".join(lines) + "\n")


def _settings(endpoint: str, **overrides: Any) -> LocalSettings:
    return LocalSettings(
        llm_endpoint=endpoint,
        llm_model="local-model",
        static_analysis_enabled=False,
        **overrides,
    )


async def _run(repo: Repo, settings: LocalSettings) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = await run_review(settings, base="main", repo_dir=repo.path, out=out, err=err)
    return status, out.getvalue(), err.getvalue()


# --- US-L12: dismiss a finding once ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_us_l12_a_dismissed_finding_is_suppressed_on_the_next_run_and_counted(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    settings = _settings(stub_llm_capture(_model([_FINDING_2, _FINDING_4])))

    status, out, err = await _run(repo, settings)  # the first run shows both
    assert status == EXIT_OK
    assert f"app.py:2: high: {_MESSAGE}" in out
    assert "memory file: not found" in err
    assert "suppressed by memory: not applicable" in err  # not "0": there was no memory to apply

    _write_memory(repo, "# a false positive", _entry())

    status, out, err = await _run(repo, settings)  # the same issue, a later run

    assert status == EXIT_OK
    assert f"app.py:2: high: {_MESSAGE}" not in out.splitlines()
    assert f"app.py:4: high: {_OTHER_MESSAGE}" in out.splitlines()
    assert "1 finding(s)." in out
    # Never silent: the dismissal is named, and counted in the summary.
    assert f"Suppressed by {DEFAULT_MEMORY_FILE} (1):" in out
    assert f"  app.py: {_MESSAGE}" in out
    assert "suppressed by memory: 1" in err
    assert "memory file: loaded with 1 entries" in err


@pytest.mark.asyncio
async def test_us_l12_a_message_that_differs_is_not_suppressed(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    _write_memory(repo, _entry(message=_MESSAGE + " (reworded)"), _entry(file="other.py"))

    status, out, err = await _run(repo, _settings(stub_llm_capture(_model([_FINDING_2]))))

    assert status == EXIT_OK
    assert f"app.py:2: high: {_MESSAGE}" in out.splitlines()
    assert "suppressed by memory: 0" in err
    assert "memory file: loaded with 2 entries" in err


@pytest.mark.asyncio
async def test_us_l12_a_malformed_file_exits_non_zero_naming_the_line_and_sends_nothing(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    _write_memory(
        repo, _entry(), "# ok so far", '{"file": "x.py", "message": "AKIAIOSFODNN7EXAMPLE"'
    )

    status, out, err = await _run(repo, _settings(stub_llm_capture(_model([_FINDING_2]))))

    assert status == EXIT_USAGE
    assert f"{DEFAULT_MEMORY_FILE}:3:" in err
    assert "AKIAIOSFODNN7EXAMPLE" not in err  # the line number, never the content
    assert "Traceback" not in err
    assert out == ""
    assert llm_requests == []


@pytest.mark.asyncio
async def test_us_l12_an_oversized_file_is_rejected_before_any_model_call(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    _write_memory(repo, *[_entry(file=f"f{i}.py") for i in range(5_000)])

    status, _, err = await _run(repo, _settings(stub_llm_capture(_model([_FINDING_2]))))

    assert status == EXIT_USAGE
    assert "memory file" in err and "limit" in err
    assert llm_requests == []


@pytest.mark.asyncio
async def test_us_l12_a_missing_or_disabled_file_is_reported_as_such_not_as_zero_suppressed(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    endpoint = stub_llm_capture(_model([_FINDING_2]))

    _, _, missing = await _run(repo, _settings(endpoint))
    _write_memory(repo, _entry())
    status, out, disabled = await _run(repo, _settings(endpoint, memory_file_enabled=False))

    assert "memory file: not found" in missing
    assert "memory file: disabled" in disabled
    assert "suppressed by memory: not applicable" in disabled
    assert status == EXIT_OK and f"app.py:2: high: {_MESSAGE}" in out.splitlines()


@pytest.mark.asyncio
async def test_us_l12_the_configured_path_must_stay_inside_the_repository(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    (tmp_path / "outside.jsonl").write_text(_entry() + "\n")
    settings = _settings(stub_llm_capture(_model([_FINDING_2])), memory_file="../outside.jsonl")

    status, _, err = await _run(repo, settings)

    assert status == EXIT_USAGE
    assert "relative" in err
    assert llm_requests == []


@pytest.mark.asyncio
async def test_us_l12_a_symlinked_memory_file_is_refused(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    (tmp_path / "elsewhere.jsonl").write_text(_entry() + "\n")
    (repo.path / ".review").mkdir()
    (repo.path / DEFAULT_MEMORY_FILE).symlink_to(tmp_path / "elsewhere.jsonl")

    status, _, err = await _run(repo, _settings(stub_llm_capture(_model([_FINDING_2]))))

    assert status == EXIT_USAGE
    assert "symbolic link" in err


# --- US-L12 in CI: the memory file comes from the base branch --------------------------------


def _event(tmp_path: Path) -> Path:
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps(
            {"action": "synchronize", "pull_request": {"number": 7, "head": {"sha": "a" * 40}}}
        )
    )
    return path


def _ci_settings(endpoint: str, tmp_path: Path) -> LocalSettings:
    return _settings(
        endpoint,
        github_actions=True,
        github_event_name="pull_request",
        github_token=SecretStr(TOKEN),
        github_repository="acme/widgets",
        github_event_path=_event(tmp_path),
        github_max_retry_attempts=0,
    )


@pytest.fixture
def github() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False, assert_all_mocked=False) as router:
        router.route(host="127.0.0.1").pass_through()
        repo = "/repos/acme/widgets"
        router.get(path=f"{repo}/pulls/7/comments").respond(200, json=[])
        router.get(path=f"{repo}/pulls/7/reviews/99/comments").respond(200, json=[])
        router.post(path="/graphql").respond(200, json={"data": {}})
        yield router.post(path=f"{repo}/pulls/7/reviews").mock(
            return_value=httpx.Response(200, json={"id": 99})
        )


def _base_with_memory(path: Path, *memory_lines: str) -> Repo:
    """``main`` (the base) optionally holding a memory file, and a ``feature`` branch (the pull
    request head) that rewrites ``app.py``."""
    repo = init_repo(path)
    repo.write("app.py", BASE_APP)
    if memory_lines:
        _write_memory(repo, *memory_lines)
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("app.py", FEATURE_APP)
    repo.commit("rewrite total")
    return repo


@pytest.mark.asyncio
async def test_us_l12_in_ci_the_base_branch_memory_file_suppresses_and_the_pr_comment_says_so(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, github: respx.Route
) -> None:
    repo = _base_with_memory(tmp_path / "repo", _entry())
    settings = _ci_settings(stub_llm_capture(_model([_FINDING_2, _FINDING_4])), tmp_path)

    status, out, err = await _run(repo, settings)

    assert status == EXIT_OK, err
    review = json.loads(github.calls.last.request.content)
    assert [(c["path"], c["line"]) for c in review["comments"]] == [("app.py", 4)]
    # Visible in the pull request, not only in the job log: a count and the item.
    body = review["body"].replace("\\", "")  # the body is markdown-escaped
    assert "Suppressed by the repository memory file: 1." in body
    assert f"- app.py: {_MESSAGE}" in body
    assert f"Suppressed by {DEFAULT_MEMORY_FILE} (1):" in out
    assert "suppressed by memory: 1" in err
    assert TOKEN not in out + err + review["body"]


@pytest.mark.asyncio
async def test_us_l12_in_ci_a_memory_file_added_by_the_pull_request_is_ignored(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, github: respx.Route
) -> None:
    """A PR author cannot dismiss their own findings by adding the entry in the PR."""
    repo = _base_with_memory(tmp_path / "repo")  # the base has no memory file
    repo.write(DEFAULT_MEMORY_FILE, _entry() + "\n")
    repo.commit("dismiss my own finding")
    settings = _ci_settings(stub_llm_capture(_model([_FINDING_2])), tmp_path)

    status, out, err = await _run(repo, settings)

    assert status == EXIT_OK, err
    review = json.loads(github.calls.last.request.content)
    assert [(c["path"], c["line"]) for c in review["comments"]] == [("app.py", 2)]
    assert "memory file: not found" in err
    assert "Suppressed" not in out


@pytest.mark.asyncio
async def test_us_l12_in_ci_deleting_the_entry_in_the_pull_request_does_not_unsuppress_it(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, github: respx.Route
) -> None:
    repo = _base_with_memory(tmp_path / "repo", _entry())
    repo.git("rm", "-q", DEFAULT_MEMORY_FILE)
    repo.commit("remove the memory file")
    settings = _ci_settings(stub_llm_capture(_model([_FINDING_2, _FINDING_4])), tmp_path)

    status, _, err = await _run(repo, settings)

    assert status == EXIT_OK, err
    review = json.loads(github.calls.last.request.content)
    assert [c["line"] for c in review["comments"]] == [4]
    assert "memory file: loaded with 1 entries" in err


@pytest.mark.asyncio
async def test_us_l12_in_ci_a_malformed_base_file_fails_naming_the_line(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    llm_requests: list[CapturedRequest],
    github: respx.Route,
) -> None:
    repo = _base_with_memory(tmp_path / "repo", _entry(), "oops")
    settings = _ci_settings(stub_llm_capture(_model([_FINDING_2])), tmp_path)

    status, _, err = await _run(repo, settings)

    assert status == EXIT_USAGE
    assert f"{DEFAULT_MEMORY_FILE}:2:" in err
    assert llm_requests == []
    assert github.call_count == 0


# --- US-L13: learned rules arrive as a diff I can review -------------------------------------


def _tree_state(repo: Repo) -> tuple[str, bytes]:
    return repo.git("status", "--porcelain", "--untracked-files=all"), (
        repo.path / DEFAULT_MEMORY_FILE
    ).read_bytes()


@pytest.mark.asyncio
async def test_us_l13_a_repeated_dismissal_is_proposed_as_a_diff_and_never_applied(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    _write_memory(
        repo, _entry(), _entry(file="b.py"), _entry(file="c.py"), "# one entry per dismissed file"
    )
    before = _tree_state(repo)

    status, out, err = await _run(repo, _settings(stub_llm_capture(_model([_FINDING_4]))))

    assert status == EXIT_OK, err
    assert f"Suggested change to {DEFAULT_MEMORY_FILE} (not applied" in out
    assert _tree_state(repo) == before  # nothing was written
    # The proposal is a real patch: it applies cleanly, and only when a human applies it.
    patch = "--- a/" + out.split("--- a/", 1)[1]
    (tmp_path / "proposal.patch").write_text(patch)
    subprocess.run(
        ["git", "-C", str(repo.path), "apply", "--check", str(tmp_path / "proposal.patch")],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo.path), "apply", str(tmp_path / "proposal.patch")],
        check=True,
        capture_output=True,
    )
    added = (repo.path / DEFAULT_MEMORY_FILE).read_text().splitlines()[-1]
    assert json.loads(added)["file"] == "*" and json.loads(added)["message"] == _MESSAGE


@pytest.mark.asyncio
async def test_us_l13_no_proposal_below_the_threshold_or_once_the_rule_exists(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    endpoint = stub_llm_capture(_model([_FINDING_4]))
    _write_memory(repo, _entry(), _entry(file="b.py"))

    _, out, _ = await _run(repo, _settings(endpoint))
    assert "Suggested change" not in out

    _write_memory(repo, _entry(), _entry(file="b.py"), _entry(file="c.py"), _entry(file="*"))
    _, out, _ = await _run(repo, _settings(endpoint))
    assert "Suggested change" not in out
