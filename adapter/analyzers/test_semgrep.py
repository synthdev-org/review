"""Tests for adapter.analyzers.semgrep, per the plan's Phase 5 Tests
section — the concrete Semgrep-backed ``StaticAnalyzer`` implementation,
against the real, installed ``semgrep`` package and the vendored, pinned
ruleset.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from adapter.analyzers.exceptions import RulesetIntegrityError
from adapter.analyzers.semgrep import _RULESET_PATH, SemgrepAnalyzer
from adapter.analyzers.subprocess_runner import run_analyzer_subprocess
from ingestion.diff import Diff

_RULESET_CHECKSUM_PATH = _RULESET_PATH.with_suffix(".yaml.sha256")


def test_pinned_ruleset_checksum_matches() -> None:
    """Per the plan's Assumptions section ("Pinned by content, not only by
    fetch timing"): the vendored ruleset's own sha256 is pinned in a
    sibling file, verified here on every push (this test carries no
    ``@pytest.mark.integration`` — pure file I/O against a small, committed
    repo asset, no subprocess/network) — an edit to
    ``semgrep_ruleset.yaml`` without also updating the pinned checksum
    fails this test, turning a ruleset change into a detected, deliberate
    re-pin rather than a silent drift.
    """
    actual = hashlib.sha256(_RULESET_PATH.read_bytes()).hexdigest()
    pinned = _RULESET_CHECKSUM_PATH.read_text().strip()

    assert actual == pinned, (
        "adapter/analyzers/semgrep_ruleset.yaml has changed without its pinned checksum "
        "(adapter/analyzers/semgrep_ruleset.yaml.sha256) being updated to match — re-pin "
        "deliberately if this is an intentional ruleset change"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_semgrep_analyzer_run_against_real_binary_returns_findings() -> None:
    analyzer = SemgrepAnalyzer()
    content = {"app.py": b"def run(x):\n    return eval(x)\n"}

    findings = await analyzer.run(Diff(files=()), content, timeout=60)

    assert findings
    assert all(f.source == "semgrep" for f in findings)
    assert any(f.rule_id == "review-eval-usage" for f in findings)
    # rule_id must be the ruleset's own declared id, never leaking this
    # machine's local filesystem layout (see adapter/analyzers/semgrep.py's
    # own "bare filename" docstring note).
    assert all(f.rule_id is not None and "/" not in f.rule_id for f in findings)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_semgrep_analyzer_finding_message_quotes_the_matched_source_line() -> None:
    analyzer = SemgrepAnalyzer()
    content = {"app.py": b"def run(x):\n    return eval(x)\n"}

    findings = await analyzer.run(Diff(files=()), content, timeout=60)

    assert any("eval(x)" in f.message for f in findings)


@pytest.mark.asyncio
async def test_semgrep_analyzer_run_returns_nothing_for_empty_content() -> None:
    """No real subprocess spawn (this is the unit tier): an empty `content`
    dict must short-circuit before `_argv` would otherwise end in a bare
    `--` with no positional targets, which would make Semgrep fall back to
    scanning its own materialization directory instead of doing nothing."""
    analyzer = SemgrepAnalyzer()

    findings = await analyzer.run(Diff(files=()), {}, timeout=60)

    assert findings == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_semgrep_analyzer_run_finds_nothing_in_clean_content() -> None:
    analyzer = SemgrepAnalyzer()

    findings = await analyzer.run(Diff(files=()), {"clean.py": b"x = 1\n"}, timeout=60)

    assert findings == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_semgrep_analyzer_treats_a_dash_prefixed_filename_as_a_positional_path() -> None:
    """The end-of-options ``--`` separator (``adapter/analyzers/semgrep.py``'s
    own ``_argv``) means a PR-controlled filename beginning with ``-`` is
    scanned as a positional path, never parsed as a CLI flag.
    """
    analyzer = SemgrepAnalyzer()
    content = {"-weird-name.py": b"def run(x):\n    return eval(x)\n"}

    findings = await analyzer.run(Diff(files=()), content, timeout=60)

    assert any(f.file == "-weird-name.py" for f in findings)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_semgrep_analyzer_routes_through_the_shared_retry_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirms this call site actually goes through
    ``adapter.analyzers.subprocess_runner.run_analyzer_subprocess`` — per
    the plan's Phase 5 Tests section — without re-testing that helper's own
    kill-and-reap/bounded-read mechanics here.
    """
    import adapter.analyzers.semgrep as semgrep_module

    calls: list[list[str]] = []
    original = run_analyzer_subprocess

    async def _recording(argv: list[str], **kwargs: object) -> bytes:
        calls.append(list(argv))
        return await original(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(semgrep_module, "run_analyzer_subprocess", _recording)
    analyzer = SemgrepAnalyzer()
    await analyzer.run(Diff(files=()), {"clean.py": b"x = 1\n"}, timeout=60)

    assert len(calls) == 1
    assert calls[0][0] == "semgrep"


@pytest.mark.asyncio
async def test_semgrep_analyzer_refuses_a_tampered_ruleset(tmp_path: Path) -> None:
    ruleset = tmp_path / "r.yaml"
    ruleset.write_text("rules: []\n")
    ruleset.with_suffix(".yaml.sha256").write_text(hashlib.sha256(b"other").hexdigest())

    with pytest.raises(RulesetIntegrityError):
        await SemgrepAnalyzer(ruleset).run(Diff(files=()), {"a.py": b"x=1\n"}, timeout=5)


@pytest.mark.asyncio
async def test_semgrep_analyzer_refuses_a_ruleset_with_no_pin(tmp_path: Path) -> None:
    ruleset = tmp_path / "r.yaml"
    ruleset.write_text("rules: []\n")

    with pytest.raises(RulesetIntegrityError):
        await SemgrepAnalyzer(ruleset).run(Diff(files=()), {"a.py": b"x=1\n"}, timeout=5)
