"""Integration tests for US-L15 (secrets stay out of prompts) and US-L14 (know what leaves my
machine).

Real temporary git repositories, the real registry through the CLI's own wiring, the real
``LiteLLMClient`` round trip against a local stub endpoint with request capture, and the real
``gitleaks`` binary for the batched input scan. The failure cases put a stand-in ``gitleaks`` first
on ``PATH`` so the scan really starts a subprocess that fails, hangs, or is never started.

Needs ``git`` and ``gitleaks`` on PATH; each skips with a reason when absent.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from adapter.llm.conftest import (
    CapturedRequest,
    StartCapturingStub,
    canned_completion_json,
)
from adapter.local.conftest import Repo, feature_branch_repo, init_repo, requires_git
from adapter.local.diff import LocalGitDiffFetcher
from adapter.local.egress import DRY_RUN_REPLY
from adapter.local.git import resolve_review_range
from apps.local_cli.main import EXIT_OK, EXIT_RUNTIME_FAILURE, run_review
from apps.local_cli.settings import LocalSettings
from review.diff_prompt import render_diff

pytestmark = [
    pytest.mark.integration,
    requires_git,
    pytest.mark.skipif(
        shutil.which("gitleaks") is None, reason="the gitleaks binary is not installed"
    ),
]

# Fake tokens in a shape the Gitleaks ruleset recognises; one per place they are planted.
_ADDED = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"
_REMOVED = "ghp_abcdefghijklmnopqrstuvwxyz1234567890AB"
_CONTEXT = "ghp_0987654321zyxwvutsrqponmlkjihgfedcba98"
_IN_PATH = "ghp_QWERTYUIOPasdfghjklzxcvbnm1234567890"


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("REVIEW_"):
            monkeypatch.delenv(name)


def _quiet_model(request: CapturedRequest) -> tuple[int, bytes]:
    """A model that finds nothing, and answers every stage with the dry run's own reply."""
    return 200, canned_completion_json(content=DRY_RUN_REPLY)


def _settings(endpoint: str, **extra: object) -> LocalSettings:
    return LocalSettings(
        llm_endpoint=endpoint,
        llm_model="local-model",
        static_analysis_enabled=False,
        **extra,  # type: ignore[arg-type]
    )


async def _run(
    repo: Repo, settings: LocalSettings, *, dry_run: bool = False
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = await run_review(
        settings, base="main", repo_dir=repo.path, out=out, err=err, dry_run=dry_run
    )
    return status, out.getvalue(), err.getvalue()


def _everything_sent(llm_requests: list[CapturedRequest]) -> str:
    return "\n".join(r.body.decode() for r in llm_requests)


def _secrets_repo(path: Path) -> Repo:
    """A branch that adds a secret, removes one, and has one in an unchanged context line, plus a
    file whose *name* is a secret."""
    repo = init_repo(path)
    repo.write(
        "config.py",
        f"a = 1\nold_token = '{_REMOVED}'\nb = 2\nkept_token = '{_CONTEXT}'\nc = 3\n",
    )
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write(
        "config.py",
        f"a = 1\nb = 2\nkept_token = '{_CONTEXT}'\nnew_token = '{_ADDED}'\nc = 3\n",
    )
    repo.write(f"notes/{_IN_PATH}.txt", "just a note\n")
    repo.commit("rotate")
    return repo


def _fake_gitleaks(directory: Path, script: str) -> None:
    directory.mkdir()
    binary = directory / "gitleaks"
    binary.write_text(f"#!/bin/sh\n{script}\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)


# --- US-L15: secrets stay out of prompts ---------------------------------------------------------


@pytest.mark.asyncio
async def test_us_l15_a_planted_secret_is_absent_from_every_request(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    llm_requests: list[CapturedRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    repo = _secrets_repo(tmp_path / "repo")

    status, out, err = await _run(repo, _settings(stub_llm_capture(_quiet_model)))

    assert status == EXIT_OK, err
    assert llm_requests, "the review should have called the model"
    sent = _everything_sent(llm_requests)
    for place, secret in {
        "added line": _ADDED,
        "removed line": _REMOVED,
        "context line": _CONTEXT,
        "path": _IN_PATH,
    }.items():
        assert secret not in sent, f"the secret in the {place} reached the model"
        assert secret[:12] not in sent, f"a fragment of the secret in the {place} reached the model"
    assert "[REDACTED" in sent
    # The summary reports that redaction occurred, as a count only.
    assert re.search(r"redaction: applied \(\d+ redacted\)", err)
    assert not any(secret in err + out for secret in (_ADDED, _REMOVED, _CONTEXT, _IN_PATH))
    assert list(scratch.iterdir()) == [], "the scan's scratch directory was not removed"


@pytest.mark.asyncio
async def test_us_l15_a_secret_split_by_the_size_ceiling_does_not_escape(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write("app.py", "x = 1\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("app.py", f"x = 1\ntoken = '{_ADDED}'\n" + "y = 2\n" * 200)
    repo.commit("add")
    # Put the size ceiling in the middle of the token, in the rendered diff the steps truncate.
    review_range = await resolve_review_range(repo.path, "main", environ=os.environ)
    raw = await LocalGitDiffFetcher(repo.path, review_range, environ=os.environ).fetch(
        installation_id=0, repo_full_name="local/x", pr_number=0, head_sha=review_range.head_sha
    )
    ceiling = render_diff(raw).index(_ADDED) + 12
    settings = _settings(stub_llm_capture(_quiet_model), llm_max_diff_bytes=ceiling)

    status, _, err = await _run(repo, settings)

    assert status == EXIT_OK, err
    sent = _everything_sent(llm_requests)
    assert "ghp_" not in sent, "a fragment of the secret cut by the ceiling reached the model"
    assert "truncation: truncated (diff cut from" in err  # and the cut is reported (US-L14)


@pytest.mark.asyncio
async def test_us_l15_when_redaction_fails_the_run_aborts_and_sends_nothing(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    llm_requests: list[CapturedRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    _fake_gitleaks(tmp_path / "bin", "echo scanner exploded >&2; exit 2")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    repo = feature_branch_repo(tmp_path / "repo")

    status, out, err = await _run(repo, _settings(stub_llm_capture(_quiet_model)))

    assert status == EXIT_RUNTIME_FAILURE
    assert llm_requests == []
    assert "redaction: failed, nothing sent" in err  # the summary line
    assert "error: redaction failed, nothing sent:" in err
    assert "Traceback" not in err and "scanner exploded" not in err
    assert out == ""
    assert list(scratch.iterdir()) == []


@pytest.mark.asyncio
async def test_us_l15_a_scan_that_hangs_hits_its_deadline_and_fails_closed(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    llm_requests: list[CapturedRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gitleaks(tmp_path / "bin", "exec sleep 30")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    repo = feature_branch_repo(tmp_path / "repo")

    status, _, err = await _run(
        repo, _settings(stub_llm_capture(_quiet_model), redaction_timeout_seconds=0.5)
    )

    assert status == EXIT_RUNTIME_FAILURE
    assert llm_requests == []
    assert "redaction: failed, nothing sent" in err


@pytest.mark.asyncio
async def test_us_l15_a_diff_over_the_file_count_cap_is_rejected_before_any_scan_starts(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    llm_requests: list[CapturedRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = tmp_path / "scanner-started"
    _fake_gitleaks(tmp_path / "bin", f"touch {started}; echo '[]' > \"$9\"")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    repo = init_repo(tmp_path / "repo")
    repo.write("base.txt", "b\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature")
    for i in range(6):
        repo.write(f"f{i}.txt", f"{i}\n")
    repo.commit("many small files")

    status, _, err = await _run(
        repo, _settings(stub_llm_capture(_quiet_model), redaction_max_files=5)
    )

    assert status == EXIT_RUNTIME_FAILURE
    assert not started.exists(), "the scanner was started although the cap was exceeded"
    assert llm_requests == []
    assert "6 files, over the 5-file cap" in err


@pytest.mark.asyncio
async def test_us_l15_a_deny_listed_file_is_not_sent_and_is_named_in_the_summary(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    # No secret in it at all: it is withheld because of its name.
    repo.write(".env", "DEBUG_FLAG=plainvalue12345\n")
    repo.commit("add env")

    status, _, err = await _run(repo, _settings(stub_llm_capture(_quiet_model)))

    assert status == EXIT_OK, err
    sent = _everything_sent(llm_requests)
    assert "plainvalue12345" not in sent
    assert "+    result = 0" in sent  # the rest of the diff is still reviewed
    assert "content not sent for deny-listed paths: .env" in err


def test_us_l15_there_is_no_setting_that_turns_redaction_off() -> None:
    names = set(LocalSettings.model_fields)
    assert not {n for n in names if "redact" in n and n not in _CAPS}, names
    assert not {n for n in names if "disable" in n}


_CAPS = {"redaction_max_files", "redaction_max_bytes", "redaction_timeout_seconds"}


# --- US-L14: know what leaves my machine ---------------------------------------------------------


def _parse_preview(text: str) -> tuple[int, int, dict[str, int]]:
    """(requests, total bytes, file -> bytes) from the dry-run report's text."""
    head = re.search(r"(\d+) request\(s\), (\d+) bytes in total", text)
    assert head, text
    files = {m.group(1): int(m.group(2)) for m in re.finditer(r"^  (\S+): (\d+) bytes", text, re.M)}
    return int(head.group(1)), int(head.group(2)), files


@pytest.mark.asyncio
async def test_us_l14_dry_run_sends_nothing_and_its_report_equals_a_real_runs_requests(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = _secrets_repo(tmp_path / "repo")
    endpoint = stub_llm_capture(_quiet_model)
    settings = _settings(endpoint)

    status, out, err = await _run(repo, settings, dry_run=True)

    assert status == EXIT_OK, err
    assert llm_requests == [], "a dry run sent a request"
    assert "Dry run: nothing was sent" in out
    assert endpoint in out  # the destination
    assert "findings." not in out and "No findings" not in out  # nothing posted or printed
    preview = _parse_preview(out)
    assert _ADDED not in out and _IN_PATH not in out

    status, _, err = await _run(repo, settings)  # the same fixture, for real

    assert status == EXIT_OK, err
    sent_prompts = [r.prompt_text() for r in llm_requests]
    requests, total_bytes, files = preview
    assert requests == len(llm_requests)
    assert total_bytes == sum(len(p.encode()) for p in sent_prompts)
    assert files, "the report lists no files"
    for path in files:
        assert all(f"--- {path} (" in p for p in sent_prompts), path
    assert all(r.path == "/v1/chat/completions" for r in llm_requests)


@pytest.mark.asyncio
async def test_us_l14_an_oversized_diff_states_what_was_cut_in_a_real_run_and_a_dry_run(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write("a.py", "x = 1\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("a.py", "x = 1\n" + "".join(f"y{i} = {i}\n" for i in range(200)))
    repo.write("b.py", "z = 1\n")
    repo.commit("big")
    settings = _settings(stub_llm_capture(_quiet_model), llm_max_diff_bytes=300)

    _, _, real_err = await _run(repo, settings)
    _, dry_out, _ = await _run(repo, settings, dry_run=True)

    for text in (real_err, dry_out):
        assert re.search(r"truncation: truncated \(diff cut from \d+ to \d+ bytes", text, re.I), (
            text
        )
    assert "cut at the size ceiling" in dry_out
    assert "Size ceiling: 300 bytes" in dry_out


@pytest.mark.asyncio
async def test_us_l14_a_diff_within_the_ceiling_reports_no_truncation(
    tmp_path: Path, stub_llm_capture: StartCapturingStub
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")

    _, _, err = await _run(repo, _settings(stub_llm_capture(_quiet_model)))

    assert "truncation: none" in err


@pytest.mark.asyncio
async def test_us_l14_dry_run_masks_credentials_in_the_endpoint_url(
    tmp_path: Path, stub_llm_capture: StartCapturingStub, llm_requests: list[CapturedRequest]
) -> None:
    repo = feature_branch_repo(tmp_path / "repo")
    endpoint = (
        stub_llm_capture(_quiet_model).replace("http://", "http://user:hunter2@") + "?key=tok"
    )

    status, out, err = await _run(repo, _settings(endpoint), dry_run=True)

    assert status == EXIT_OK, err
    assert llm_requests == []
    assert "hunter2" not in out + err and "tok" not in out.replace("token", "")


# --- audit fixes: the byte cap and the scratch directory's mode -----------------------------------


@pytest.mark.asyncio
async def test_us_l15_a_diff_over_the_byte_cap_is_rejected_before_any_scan_starts(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    llm_requests: list[CapturedRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = tmp_path / "scanner-started"
    _fake_gitleaks(tmp_path / "bin", f"touch {started}; echo '[]' > \"$9\"")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    repo = init_repo(tmp_path / "repo")
    repo.write("base.txt", "b\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("big.txt", "".join(f"line {i} of a large file\n" for i in range(400)))
    repo.commit("one large file")

    status, out, err = await _run(
        repo, _settings(stub_llm_capture(_quiet_model), redaction_max_bytes=1_000)
    )

    assert status == EXIT_RUNTIME_FAILURE
    assert not started.exists(), "the scanner was started although the cap was exceeded"
    assert llm_requests == [] and out == ""
    assert "-byte cap" in err and "redaction: failed, nothing sent" in err


@pytest.mark.asyncio
async def test_us_l15_the_scan_runs_in_a_mode_0700_scratch_directory_removed_afterwards(
    tmp_path: Path,
    stub_llm_capture: StartCapturingStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    seen = tmp_path / "scratch-mode"
    # The scanner runs with the scratch directory as its working directory.
    _fake_gitleaks(tmp_path / "bin", f'stat -c %a "$PWD" > {seen}; echo \'[]\' > "$9"')
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    repo = feature_branch_repo(tmp_path / "repo")

    status, _, err = await _run(repo, _settings(stub_llm_capture(_quiet_model)))

    assert status == EXIT_OK, err
    assert seen.read_text().strip() == "700"
    assert list(scratch.iterdir()) == []
