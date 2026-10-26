"""Integration tests for US-L1 (review my branch) and US-L4 (works without setting anything up).

The whole local pipeline runs for real: real temporary git repositories, the real registry built by
``review.registry`` through the CLI's own wiring, the real ``LiteLLMClient`` round trip against a
local stub endpoint (the recorded model, with request capture from adapter/llm/conftest.py), and the
real ``gitleaks`` binary for redaction. No database, queue, server or container runtime exists in
these tests, which is the point of US-L4.

Needs ``git`` and ``gitleaks`` on PATH; each skips with a reason when absent.
"""

from __future__ import annotations

import gc
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from adapter.llm.conftest import (
    CapturedRequest,
    StartCapturingStub,
    canned_completion_json,
    unused_port_endpoint,
)
from adapter.local.conftest import (
    Repo,
    ci_merge_ref_repo,
    detached_head_repo,
    feature_branch_repo,
    init_repo,
    requires_git,
    shallow_clone_without_base,
)
from apps.local_cli import main as cli
from apps.local_cli.main import EXIT_OK, EXIT_RUNTIME_FAILURE, EXIT_USAGE, run_review
from apps.local_cli.settings import LocalSettings

pytestmark = [
    pytest.mark.integration,
    requires_git,
    pytest.mark.skipif(
        shutil.which("gitleaks") is None, reason="the gitleaks binary is not installed"
    ),
]

_FINDING = "app.py:2: high: result starts as an int, so a list of floats mixes types"


def _model_reply(request: CapturedRequest) -> tuple[int, bytes]:
    """The recorded model: answers each pipeline stage the way a real one would."""
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
        content = "Rewrites total() as an explicit accumulation loop."
    else:
        content = json.dumps(
            {
                "findings": [
                    {
                        "file": "app.py",
                        "line": 2,
                        "severity": "high",
                        "message": "result starts as an int, so a list of floats mixes types",
                    }
                ]
            }
        )
    return 200, canned_completion_json(content=content)


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only what a test sets is configured: no worker settings, database URL or stray model."""
    for name in list(os.environ):
        if name.startswith("REVIEW_"):
            monkeypatch.delenv(name)


def _settings(
    endpoint: str, *, static_analysis_enabled: bool = False, llm_timeout_seconds: float = 30.0
) -> LocalSettings:
    # Analyzers are off unless a test asks: only gitleaks is guaranteed to be installed.
    return LocalSettings(
        llm_endpoint=endpoint,
        llm_model="local-model",
        static_analysis_enabled=static_analysis_enabled,
        llm_timeout_seconds=llm_timeout_seconds,
    )


async def _run(repo: Repo, settings: LocalSettings, base: str = "main") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = await run_review(settings, base=base, repo_dir=repo.path, out=out, err=err)
    return status, out.getvalue(), err.getvalue()


# --- US-L1: review my branch before I push -------------------------------------------------------


@pytest.mark.asyncio
async def test_us_l1_prints_findings_for_the_changed_lines(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    settings = _settings(stub_llm_capture(_model_reply))

    status, out, err = await _run(repo, settings)

    assert status == EXIT_OK
    assert "Run summary" in err and "Traceback" not in err
    assert out.splitlines() == [_FINDING, "1 finding(s)."]
    # The model was given the branch's change (the diff against the merge base) to review.
    review_prompt = next(r.prompt_text() for r in llm_requests if "findings" in r.prompt_text())
    assert "+    result = 0" in review_prompt
    assert "-    return sum(items)" in review_prompt


@pytest.mark.asyncio
async def test_us_l1_with_the_default_analyzers_enabled(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    """The shipped default: every analyzer registered. Those not installed here fail alone
    (isolated) and the review still completes."""
    repo = feature_branch_repo(tmp_path / "repo")
    settings = _settings(stub_llm_capture(_model_reply), static_analysis_enabled=True)

    status, out, _ = await _run(repo, settings)

    assert status == EXIT_OK
    assert _FINDING in out.splitlines()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["detached_head", "ci_merge_ref"])
async def test_us_l1_works_on_detached_heads_and_ci_merge_refs(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, shape: str
) -> None:
    build = {"detached_head": detached_head_repo, "ci_merge_ref": ci_merge_ref_repo}[shape]
    repo = build(tmp_path / "repo")

    status, out, err = await _run(repo, _settings(stub_llm_capture(_model_reply)))

    assert status == EXIT_OK
    assert "Run summary" in err and "Traceback" not in err
    assert _FINDING in out.splitlines()


@pytest.mark.asyncio
async def test_us_l1_nothing_to_review_exits_0_without_calling_the_model(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write("a.txt", "a\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature")

    status, out, _ = await _run(repo, _settings(stub_llm_capture(_model_reply)))

    assert status == EXIT_OK
    assert "Nothing to review" in out
    assert llm_requests == []


@pytest.mark.asyncio
async def test_us_l1_a_missing_base_ref_exits_non_zero_naming_it(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")

    status, out, err = await _run(
        repo, _settings(stub_llm_capture(_model_reply)), base="no-such-branch"
    )

    assert status == EXIT_USAGE
    assert "no-such-branch" in err
    assert out == ""
    assert llm_requests == []


@pytest.mark.asyncio
async def test_us_l1_a_shallow_ci_checkout_missing_the_base_says_how_to_fix_it(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    origin = feature_branch_repo(tmp_path / "origin")
    shallow = shallow_clone_without_base(origin, tmp_path / "shallow")

    status, _, err = await _run(
        shallow, _settings(stub_llm_capture(_model_reply)), base="origin/main"
    )

    assert status == EXIT_USAGE
    assert "origin/main" in err
    assert "shallow clone" in err
    assert llm_requests == []


@pytest.mark.asyncio
async def test_us_l1_an_unreachable_model_fails_visibly_and_presents_no_findings(
    tmp_path: Path,
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    endpoint = unused_port_endpoint()

    status, out, err = await _run(repo, _settings(endpoint, llm_timeout_seconds=5))

    assert status == EXIT_RUNTIME_FAILURE
    assert endpoint in err
    assert "Traceback" not in err
    assert out == ""  # no "No findings." that could be mistaken for a clean review


@pytest.mark.asyncio
async def test_us_l1_refuses_to_send_anything_when_redaction_is_unavailable(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    llm_requests: list[CapturedRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    monkeypatch.setattr("apps.local_cli.main.shutil.which", lambda _name, path=None: None)

    status, _, err = await _run(repo, _settings(stub_llm_capture(_model_reply)))

    assert status == EXIT_USAGE
    assert "gitleaks" in err
    assert llm_requests == []


# --- US-L4: works without setting anything up ----------------------------------------------------


def test_us_l4_a_review_completes_with_only_a_checkout_and_a_model(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Through the real entry point, configured by environment alone: nothing else exists - no
    database URL, no GitHub credentials, no worker settings."""
    repo = feature_branch_repo(tmp_path / "repo")
    monkeypatch.chdir(repo.path)
    monkeypatch.setenv("REVIEW_LOCAL_LLM_ENDPOINT", stub_llm_capture(_model_reply))
    monkeypatch.setenv("REVIEW_LOCAL_LLM_MODEL", "local-model")
    monkeypatch.setenv("REVIEW_LOCAL_STATIC_ANALYSIS_ENABLED", "false")
    assert not any(name.startswith("REVIEW_") and "LOCAL" not in name for name in os.environ)
    assert "REVIEW_DATABASE_URL" not in os.environ

    # Flush GC noise (leaked litellm tasks) from earlier tests; it is not this CLI's output.
    gc.collect()
    capsys.readouterr()

    status = cli.main(["run", "--base", "main"])

    captured = capsys.readouterr()
    assert status == EXIT_OK
    assert "Run summary" in captured.err and "Traceback" not in captured.err
    assert _FINDING in captured.out.splitlines()


def test_us_l4_the_cli_never_imports_the_worker_or_the_database_adapter() -> None:
    """A fresh interpreter, so other tests' imports cannot hide a dependency. ``apps.worker`` is
    the other composition root; ``adapter.storage`` is the Postgres adapter."""
    probe = (
        "import sys, apps.local_cli.main\n"
        "bad = [m for m in ('apps.worker', 'apps.webhook_api', 'adapter.storage', "
        "'adapter.github.auth', 'asyncpg') if m in sys.modules]\n"
        "sys.exit(f'imported: {bad}' if bad else 0)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_us_l4_no_model_configured_exits_non_zero_listing_the_options(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    llm_requests: list[CapturedRequest],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    stub_llm_capture(_model_reply)  # a reachable model exists, but nothing points at it
    monkeypatch.chdir(repo.path)

    # Flush GC noise (leaked litellm tasks) from earlier tests; it is not this CLI's output.
    gc.collect()
    capsys.readouterr()

    status = cli.main(["run", "--base", "main"])

    captured = capsys.readouterr()
    assert status == EXIT_USAGE
    assert "no model is configured" in captured.err
    assert "REVIEW_LOCAL_LLM_MODEL" in captured.err
    assert "REVIEW_LOCAL_LLM_ENDPOINT" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""
    assert llm_requests == []
