"""Tests for adapter.analyzers.gitleaks, per the plan's Phase 5 Tests
section — the concrete Gitleaks-backed ``StaticAnalyzer``/``TextRedactor``
implementations, against the real vendored ``gitleaks`` binary.
"""

from __future__ import annotations

import pytest

from adapter.analyzers.exceptions import AnalyzerInvocationError
from adapter.analyzers.gitleaks import GitleaksAnalyzer, GitleaksTextRedactor
from adapter.analyzers.subprocess_runner import run_analyzer_subprocess
from ingestion.diff import Diff

_PLANTED_SECRET = 'github_token = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"'


@pytest.mark.asyncio
@pytest.mark.integration
async def test_redact_masks_a_known_fake_secret_via_a_real_subprocess_call() -> None:
    redactor = GitleaksTextRedactor()

    redacted = await redactor.redact(_PLANTED_SECRET)

    assert "ghp_1234567890abcdefghijklmnopqrstuvwxyz12" not in redacted
    assert redacted != _PLANTED_SECRET


@pytest.mark.asyncio
@pytest.mark.integration
async def test_redact_leaves_text_with_no_detectable_secret_unchanged() -> None:
    redactor = GitleaksTextRedactor()

    redacted = await redactor.redact("nothing secret on this line")

    assert redacted == "nothing secret on this line"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_redact_propagates_a_fatal_error_not_a_silent_pass_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A companion negative case, per the plan's Phase 5 Tests section: the
    Gitleaks binary given a deliberately-invalid invocation (an unknown
    flag, forcing a genuine non-zero exit) asserts the fatal-failure
    behavior — never a silent pass-through returning the candidate text
    unchanged, which would be a worse outcome than failing the job (per
    this mechanism's own fail-closed design).
    """

    def _broken_argv(report_path: object) -> list[str]:
        return ["gitleaks", "--this-flag-does-not-exist"]

    monkeypatch.setattr("adapter.analyzers.gitleaks._argv", _broken_argv)

    redactor = GitleaksTextRedactor()
    with pytest.raises(AnalyzerInvocationError):
        await redactor.redact(_PLANTED_SECRET)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_gitleaks_analyzer_run_against_real_binary_returns_findings() -> None:
    analyzer = GitleaksAnalyzer()

    findings = await analyzer.run(
        Diff(files=()), {"secret.py": _PLANTED_SECRET.encode()}, timeout=30, config_files={}
    )

    assert findings
    assert all(f.source == "gitleaks" for f in findings)
    assert all(f.file == "secret.py" for f in findings)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_gitleaks_analyzer_reports_the_original_path_for_a_backslash_filename() -> None:
    """`materialize_into_temp_directory` writes a backslash-containing path
    under its forward-slash-normalized form; Gitleaks reports that
    normalized path back, so `GitleaksAnalyzer.run` must map it back to the
    original `content` key (same as `SemgrepAnalyzer.run` already does) or
    a posted review comment lands on a path GitHub's diff never had.
    """
    analyzer = GitleaksAnalyzer()

    findings = await analyzer.run(
        Diff(files=()), {"weird\\secret.py": _PLANTED_SECRET.encode()}, timeout=30, config_files={}
    )

    assert findings
    assert all(f.file == "weird\\secret.py" for f in findings)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_gitleaks_analyzer_run_finds_nothing_in_clean_content() -> None:
    analyzer = GitleaksAnalyzer()

    findings = await analyzer.run(
        Diff(files=()), {"clean.py": b"x = 1\n"}, timeout=30, config_files={}
    )

    assert findings == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_gitleaks_analyzer_routes_through_the_shared_retry_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirms this call site actually goes through
    ``adapter.analyzers.subprocess_runner.run_analyzer_subprocess`` — per
    the plan's Phase 5 Tests section — mirroring how ``adapter/github``'s
    own call sites each confirm they route through their shared helper,
    without re-testing that helper's own kill-and-reap/bounded-read
    mechanics here (``adapter/analyzers/test_subprocess_runner.py``'s job).
    """
    import adapter.analyzers.gitleaks as gitleaks_module

    calls: list[list[str]] = []
    original = run_analyzer_subprocess

    async def _recording(argv: list[str], **kwargs: object) -> bytes:
        calls.append(list(argv))
        return await original(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gitleaks_module, "run_analyzer_subprocess", _recording)
    analyzer = GitleaksAnalyzer()
    await analyzer.run(Diff(files=()), {"clean.py": b"x = 1\n"}, timeout=30, config_files={})

    assert len(calls) == 1
    assert calls[0][0] == "gitleaks"
