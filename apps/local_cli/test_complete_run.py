"""Integration tests for US-L2 (review only what is staged), US-L3 (keep the findings) and US-L5
(deterministic checks first), plus the run summary, progress and exit codes they share
(docs/plans/plan-local-mode.md, Phase 2).

Real temporary git repositories, the real registry through the CLI's own wiring, the recorded model
on a local stub endpoint with request capture, the real ``gitleaks`` and ``ruff`` binaries. Needs
``git``, ``gitleaks`` and ``ruff`` on PATH; each skips with a reason when absent.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

import jsonschema
import pytest

from adapter.llm.conftest import (
    CapturedRequest,
    RequestResponder,
    StartCapturingStub,
    canned_completion_json,
    unused_port_endpoint,
)
from adapter.local.conftest import Repo, feature_branch_repo, init_repo, requires_git
from adapter.local.report import Report
from apps.local_cli.main import (
    EXIT_OK,
    EXIT_RUNTIME_FAILURE,
    EXIT_USAGE,
    run_review,
)
from apps.local_cli.settings import LocalSettings

pytestmark = [
    pytest.mark.integration,
    requires_git,
    pytest.mark.skipif(
        shutil.which("gitleaks") is None, reason="the gitleaks binary is not installed"
    ),
]

_SCHEMA = Path(__file__).resolve().parents[2] / "docs" / "reference" / "review-report.schema.json"
_FINDING = "app.py:2: high: result starts as an int, so a list of floats mixes types"


def _reply(file: str, line: int, message: str) -> RequestResponder:
    """A recorded model that answers each pipeline stage and reports one finding."""

    def respond(request: CapturedRequest) -> tuple[int, bytes]:
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
            content = "Changes the code under review."
        else:
            content = json.dumps(
                {"findings": [{"file": file, "line": line, "severity": "high", "message": message}]}
            )
        return 200, canned_completion_json(content=content)

    return respond


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("REVIEW_"):
            monkeypatch.delenv(name)


def _settings(endpoint: str, *, static_analysis_enabled: bool = False) -> LocalSettings:
    return LocalSettings(
        llm_endpoint=endpoint,
        llm_model="local-model",
        static_analysis_enabled=static_analysis_enabled,
        llm_timeout_seconds=5,
    )


async def _run(
    repo: Repo,
    settings: LocalSettings,
    *,
    base: str | None = "main",
    staged: bool = False,
    output: Path | None = None,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = await run_review(
        settings,
        base=None if staged else base,
        staged=staged,
        output=output,
        repo_dir=repo.path,
        out=out,
        err=err,
    )
    return status, out.getvalue(), err.getvalue()


# --- US-L2: review only what I have staged --------------------------------------------------------


def _staged_and_unstaged_repo(path: Path) -> Repo:
    repo = init_repo(path)
    repo.write("app.py", "def total(items):\n    return sum(items)\n")
    repo.commit("base")
    # Staged: line 4 is new. Then an unstaged edit adds two lines above it, so the working-tree
    # line number of the same code differs from the staged one.
    staged = "def total(items):\n    return sum(items)\n\nSTAGED = 1\n"
    repo.write("app.py", staged)
    repo.git("add", "app.py")
    repo.write("app.py", "# unstaged header\n# unstaged header 2\n" + staged + "UNSTAGED = 2\n")
    return repo


@pytest.mark.asyncio
async def test_us_l2_reviews_only_the_staged_hunks_and_anchors_findings_to_staged_lines(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = _staged_and_unstaged_repo(tmp_path / "repo")
    endpoint = stub_llm_capture(_reply("app.py", 4, "STAGED is a magic number"))

    status, out, err = await _run(repo, _settings(endpoint), staged=True)

    assert status == EXIT_OK
    assert "app.py:4: high: STAGED is a magic number" in out.splitlines()
    review_prompt = next(r.prompt_text() for r in llm_requests if "Diff:" in r.prompt_text())
    assert "+STAGED = 1" in review_prompt
    assert "unstaged header" not in review_prompt
    assert "UNSTAGED" not in review_prompt
    assert "Traceback" not in err


@pytest.mark.asyncio
async def test_us_l2_nothing_staged_means_nothing_to_review_and_no_model_call(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write("a.txt", "a\n")
    repo.commit("base")
    repo.write("a.txt", "a changed but not staged\n")

    status, out, err = await _run(
        repo, _settings(stub_llm_capture(_reply("a.txt", 1, "x"))), staged=True
    )

    assert status == EXIT_OK
    assert "Nothing to review: no staged changes." in out
    assert "Run summary" in err  # the quickstart documents the summary on stderr even here
    assert llm_requests == []


@pytest.mark.asyncio
async def test_us_l2_staged_in_a_repository_with_no_commits_is_a_usage_error(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write("a.txt", "a\n")
    repo.git("add", "a.txt")

    status, _, err = await _run(
        repo, _settings(stub_llm_capture(_reply("a.txt", 1, "x"))), staged=True
    )

    assert status == EXIT_USAGE
    assert "HEAD" in err
    assert llm_requests == []


# --- US-L3: keep the findings ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_us_l3_json_output_has_the_terminal_findings_and_validates_against_the_schema(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    target = tmp_path / "review.json"
    endpoint = stub_llm_capture(_reply("app.py", 2, "result starts as an int"))

    status, out, _ = await _run(repo, _settings(endpoint), output=target)

    assert status == EXIT_OK
    document = json.loads(target.read_text())
    jsonschema.validate(document, json.loads(_SCHEMA.read_text()))
    assert document["schema_version"] == "1.0"
    assert [(f["file"], f["line"], f["message"]) for f in document["findings"]] == [
        ("app.py", 2, "result starts as an int")
    ]
    assert "app.py:2: high: result starts as an int" in out
    # The run summary travels with the findings.
    assert document["summary"]["backend"]["model"] == "local-model"
    assert {s["name"] for s in document["summary"]["stages"]} >= {"prefilter", "review_generation"}
    assert Report.model_validate(document).findings[0].file == "app.py"


@pytest.mark.asyncio
async def test_us_l3_markdown_output_has_the_same_findings_and_neutralises_markup(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    target = tmp_path / "review.md"
    hostile = "[click](http://evil.example) @someone ![img](http://evil.example/x.png)"
    endpoint = stub_llm_capture(_reply("app.py", 2, hostile))

    status, _, _ = await _run(repo, _settings(endpoint), output=target)

    text = target.read_text()
    assert status == EXIT_OK
    assert "app\\.py:2" in text
    assert "[high]" in text
    assert "[click](http://evil.example)" not in text  # brackets and parentheses are escaped
    assert "![img]" not in text
    assert "@someone" not in text  # the mention is broken with a zero-width space


@pytest.mark.asyncio
async def test_us_l3_nothing_to_review_still_writes_a_valid_empty_report(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write("a.txt", "a\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature")
    target = tmp_path / "review.json"

    status, _, _ = await _run(
        repo, _settings(stub_llm_capture(_reply("a.txt", 1, "x"))), output=target
    )

    assert status == EXIT_OK
    document = json.loads(target.read_text())
    jsonschema.validate(document, json.loads(_SCHEMA.read_text()))
    assert document["findings"] == []


@pytest.mark.asyncio
async def test_us_l3_an_unwritable_path_fails_before_the_model_is_called(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    settings = _settings(stub_llm_capture(_reply("app.py", 2, "x")))

    missing_dir = await _run(repo, settings, output=tmp_path / "no-such-dir" / "review.md")
    (tmp_path / "dir.json").mkdir()
    a_directory = await _run(repo, settings, output=tmp_path / "dir.json")
    wrong_extension = await _run(repo, settings, output=tmp_path / "review.txt")

    for status, out, err in (missing_dir, a_directory, wrong_extension):
        assert status == EXIT_USAGE
        assert "--output" in err
        assert out == ""
        assert "Traceback" not in err
    assert llm_requests == []


@contextlib.contextmanager
def _unprivileged(tmp_path: Path) -> Iterator[None]:
    """Root ignores directory modes, so drop to ``nobody`` for the run (no-op when not root)."""
    if os.geteuid() != 0:
        yield
        return
    for path in (tmp_path, *tmp_path.parents):  # let ``nobody`` traverse to the repo
        if path == path.parent:
            break
        path.chmod(path.stat().st_mode | stat.S_IXOTH | stat.S_IROTH)
    os.setgroups([])  # root's supplementary group would otherwise match the group class
    os.setresgid(65534, 65534, 0)
    os.setresuid(65534, 65534, 0)
    try:
        yield
    finally:
        os.setresuid(0, 0, 0)
        os.setresgid(0, 0, 0)


@pytest.mark.asyncio
async def test_us_l3_a_read_only_directory_fails_before_the_model_is_called(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    read_only = tmp_path / "ro"
    read_only.mkdir()
    read_only.chmod(0o555)  # r-x for everyone, so the dropped-privilege user can still look inside
    settings = _settings(stub_llm_capture(_reply("app.py", 2, "x")))

    with _unprivileged(tmp_path):
        status, _, err = await _run(repo, settings, output=read_only / "r.md")

    assert status == EXIT_USAGE
    assert "not writable" in err
    assert llm_requests == []


@pytest.mark.asyncio
async def test_us_l3_refuses_a_symlink_and_leaves_its_target_alone(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    link = tmp_path / "review.md"
    link.symlink_to(victim)

    status, _, err = await _run(
        repo, _settings(stub_llm_capture(_reply("app.py", 2, "x"))), output=link
    )

    assert status == EXIT_USAGE
    assert "symbolic link" in err
    assert victim.read_text() == "precious"
    assert llm_requests == []


@pytest.mark.asyncio
async def test_us_l3_never_silently_overwrites_a_tracked_file(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    tracked = repo.path / "README.md"
    repo.write("notes.json", "{}\n")
    repo.commit("track notes")
    settings = _settings(stub_llm_capture(_reply("app.py", 2, "x")))

    markdown = await _run(repo, settings, output=tracked)
    json_file = await _run(repo, settings, output=repo.path / "notes.json")

    for status, _, err in (markdown, json_file):
        assert status == EXIT_USAGE
        assert "tracked by git" in err
    assert tracked.read_text() == "# demo\n"
    assert (repo.path / "notes.json").read_text() == "{}\n"
    assert llm_requests == []


@pytest.mark.asyncio
async def test_us_l3_an_untracked_previous_output_is_replaced(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    target = repo.path / "review.md"
    target.write_text("old")

    status, _, _ = await _run(
        repo, _settings(stub_llm_capture(_reply("app.py", 2, "x"))), output=target
    )

    assert status == EXIT_OK
    assert target.read_text().startswith("# Code review")


@pytest.mark.asyncio
async def test_us_l3_a_failed_run_writes_no_file(tmp_path: Path) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    target = tmp_path / "review.json"

    status, _, err = await _run(repo, _settings(unused_port_endpoint()), output=target)

    assert status == EXIT_RUNTIME_FAILURE
    assert not target.exists()
    assert "a review stage failed" in err


# --- US-L5: deterministic checks first ------------------------------------------------------------

_UNUSED_IMPORT = "import os\n\n\ndef total(items):\n    return sum(items)\n"

needs_ruff = pytest.mark.skipif(shutil.which("ruff") is None, reason="the ruff binary is missing")


def _repo_with_unused_import(path: Path) -> Repo:
    repo = init_repo(path)
    repo.write("README.md", "# demo\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("mod.py", _UNUSED_IMPORT)
    repo.commit("add mod")
    return repo


@needs_ruff
@pytest.mark.asyncio
async def test_us_l5_an_installed_analyzer_finding_is_reported_and_given_to_the_model(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = _repo_with_unused_import(tmp_path / "repo")
    endpoint = stub_llm_capture(_reply("mod.py", 5, "sum() of an empty list is 0"))

    status, out, err = await _run(repo, _settings(endpoint, static_analysis_enabled=True))

    assert status == EXIT_OK
    assert "mod.py:1:" in out and "(F401)" in out
    review_prompt = next(
        r.prompt_text() for r in llm_requests if "Static-analysis findings" in r.prompt_text()
    )
    assert "F401" in review_prompt  # supplied as ground truth, as in server mode
    assert "used ruff" in err.replace("\n", " ")


@needs_ruff
@pytest.mark.asyncio
async def test_us_l5_a_missing_binary_is_skipped_and_named_in_the_summary(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo_with_unused_import(tmp_path / "repo")
    real_which = shutil.which
    monkeypatch.setattr(
        "apps.local_cli.main.shutil.which",
        lambda name, path=None: None if name == "semgrep" else real_which(name, path=path),
    )
    target = tmp_path / "review.json"

    status, out, err = await _run(
        repo,
        _settings(stub_llm_capture(_reply("mod.py", 5, "x")), static_analysis_enabled=True),
        output=target,
    )

    assert status == EXIT_OK  # the review continued without it
    assert "(F401)" in out
    assert "skipped semgrep (not installed)" in err
    analyzers = {a["name"]: a for a in json.loads(target.read_text())["summary"]["analyzers"]}
    assert analyzers["semgrep"] == {
        "name": "semgrep",
        "status": "skipped",
        "reason": "not installed",
    }
    assert analyzers["ruff"]["status"] == "used"


@needs_ruff
@pytest.mark.asyncio
async def test_us_l5_an_analyzer_that_is_present_but_crashes_is_errored_not_skipped(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_unused_import(tmp_path / "repo")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    crashing = fake_bin / "ruff"
    crashing.write_text("#!/bin/sh\necho 'boom: secret-looking-output' >&2\nexit 3\n")
    crashing.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    target = tmp_path / "review.json"

    status, out, err = await _run(
        repo,
        _settings(stub_llm_capture(_reply("mod.py", 5, "x")), static_analysis_enabled=True),
        output=target,
    )

    assert status == EXIT_OK  # an analyzer failure is isolated; the model review still runs
    assert "mod.py:5" in out
    assert "errored ruff (failed)" in err
    assert "boom" not in err  # the analyzer's output is never echoed
    analyzers = {a["name"]: a for a in json.loads(target.read_text())["summary"]["analyzers"]}
    assert analyzers["ruff"]["status"] == "errored"


@pytest.mark.asyncio
async def test_us_l5_with_static_analysis_off_every_analyzer_is_reported_skipped(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")

    status, _, err = await _run(repo, _settings(stub_llm_capture(_reply("app.py", 2, "x"))))

    assert status == EXIT_OK
    assert err.count("static analysis disabled") == 7


# --- the run summary, progress and failure reporting ----------------------------------------------


@pytest.mark.asyncio
async def test_every_run_ends_with_a_plain_text_summary_with_progress_before_it(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")

    status, out, err = await _run(repo, _settings(stub_llm_capture(_reply("app.py", 2, "x"))))

    assert status == EXIT_OK
    lines = err.splitlines()
    summary_at = lines.index("Run summary")
    progress = "\n".join(lines[:summary_at])
    for stage in ("prefilter", "review_generation", "verification", "output"):
        assert f"  {stage} ..." in progress
        assert f"  {stage}: success" in progress
    summary = "\n".join(lines[summary_at:])
    assert "backend: openai_compatible, model local-model" in summary
    assert "token usage:" in summary
    for field in ("redaction:", "truncation:", "mcp servers:", "memory file:", "ci comments:"):
        assert field in summary
    # Plain text: no colour or other terminal control sequences, on either stream.
    assert "\x1b" not in out and "\x1b" not in err


@pytest.mark.asyncio
async def test_a_runtime_failure_gives_a_reason_and_the_summary_but_no_traceback_or_findings(
    tmp_path: Path,
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    endpoint = unused_port_endpoint()

    status, out, err = await _run(repo, _settings(endpoint))

    assert status == EXIT_RUNTIME_FAILURE
    assert out == ""
    assert "error: a review stage failed:" in err
    assert "Run summary" in err
    assert "Traceback" not in err
    assert "intent_summary: failed" in err


@pytest.mark.asyncio
async def test_a_runtime_failure_line_leaks_no_cause_text(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    canary = "CANARY-SECRET-BODY\x1b[31mINJECT"
    endpoint = stub_llm_capture(lambda request: (500, canary.encode()))
    repo = feature_branch_repo(tmp_path / "repo")

    status, _, err = await _run(repo, _settings(endpoint))

    assert status == EXIT_RUNTIME_FAILURE
    assert "error: a review stage failed:" in err
    assert "CANARY" not in err and "\x1b" not in err


@pytest.mark.asyncio
async def test_us_l3_markdown_output_carries_the_message_text_severity_and_location(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    target = tmp_path / "review.md"
    endpoint = stub_llm_capture(_reply("app.py", 2, "result starts as an int"))

    status, out, _ = await _run(repo, _settings(endpoint), output=target)

    assert status == EXIT_OK
    assert target.read_text().splitlines() == [
        "# Code review",
        "",
        "1 finding(s).",
        "",
        "- **app\\.py:2** **[high]** result starts as an int",
    ]
    assert "app.py:2: high: result starts as an int" in out
