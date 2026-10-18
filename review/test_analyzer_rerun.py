"""Tests for review.analyzer_rerun (plan-phase-11 Phase 4, US-AF6): the real
pinned Ruff, ESLint, PHPStan and golangci-lint binaries on fixtures at the
integration tier; crash, timeout, caching and slot bounding with counting
fakes around the same helper.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import TypedDict

import pytest

from adapter.analyzers.eslint import ESLINT_CONFIG_FILENAMES, EslintAnalyzer
from adapter.analyzers.exceptions import AnalyzerInvocationError
from adapter.analyzers.golangci_lint import GOLANGCI_CONFIG_FILENAMES, GolangciLintAnalyzer
from adapter.analyzers.phpstan import PHPSTAN_CONFIG_FILENAMES, PhpstanAnalyzer
from adapter.analyzers.ruff import RUFF_CONFIG_FILENAMES, RuffAnalyzer
from findings.models import Finding
from ingestion.diff import Diff
from review.analyzer_rerun import AnalyzerRerunner, ReanalysisResult
from review.file_matchers import match_go, match_javascript, match_php, match_python
from review.fix_generation import PatchReanalyzer
from review.static_analysis import AnalyzerRegistration, StaticAnalyzer


class _Job(TypedDict):
    installation_id: int
    repo_full_name: str
    head_sha: str


_JOB = _Job(installation_id=1, repo_full_name="o/r", head_sha="abc")


@pytest.fixture(autouse=True)
def _enable_loggers() -> None:
    # An earlier test's logging setup may have disabled these already-imported loggers.
    for name in ("review.analyzer_rerun", "review.static_analysis"):
        logging.getLogger(name).disabled = False


def _real_registrations() -> list[AnalyzerRegistration]:
    return [
        AnalyzerRegistration(
            "ruff", RuffAnalyzer(), matches=match_python, config_filenames=RUFF_CONFIG_FILENAMES
        ),
        AnalyzerRegistration(
            "eslint",
            EslintAnalyzer(),
            matches=match_javascript,
            config_filenames=ESLINT_CONFIG_FILENAMES,
        ),
        AnalyzerRegistration(
            "phpstan",
            PhpstanAnalyzer(),
            matches=match_php,
            config_filenames=PHPSTAN_CONFIG_FILENAMES,
        ),
        AnalyzerRegistration(
            "golangci_lint",
            GolangciLintAnalyzer(),
            matches=match_go,
            config_filenames=GOLANGCI_CONFIG_FILENAMES,
        ),
    ]


def _rerunner(
    registrations: list[AnalyzerRegistration] | None = None,
    *,
    slots: int = 4,
    timeout: float = 120.0,
    config_fetcher: _ConfigFetcher | None = None,
) -> AnalyzerRerunner:
    return AnalyzerRerunner(
        registrations or _real_registrations(),
        asyncio.Semaphore(slots),
        {"static_analysis_analyzer": timeout},
        config_fetcher,
    )


@dataclass(frozen=True)
class _Case:
    analyzer: str
    path: str
    rule_id: str
    original: bytes
    cleared: bytes
    cleared_span: tuple[int, int]
    left: bytes
    introduces: bytes


_CASES = [
    # Clearing deletes line 1-3, so the remaining F821 shifts from line 5 to 2.
    _Case(
        "ruff",
        "a.py",
        "F401",
        b"import os\n\n\ndef f():\n    return undefined_name\n",
        b"def f():\n    return undefined_name\n",
        (1, 1),
        b"import os\n\n\ndef f():\n    return undefined_name + 1\n",
        b"def f():\n    return undefined_name + other_name\n",
    ),
    _Case(
        "eslint",
        "a.js",
        "no-unused-vars",
        b"export const a = 1;\nconst unused = 2;\n"
        b"export function f() {\n  return undefinedName;\n}\n",
        b"export const a = 1;\nexport function f() {\n  return undefinedName;\n}\n",
        (2, 2),
        b"export const a = 1;\nconst unused = 3;\n"
        b"export function f() {\n  return undefinedName;\n}\n",
        b"export const a = 1;\nexport function f() {\n  return undefinedName + other;\n}\n",
    ),
    _Case(
        "phpstan",
        "a.php",
        "variable.undefined",
        b"<?php\nfunction f() {\n    return $a;\n}\n\nfunction g() {\n    return $b;\n}\n",
        b"<?php\nfunction f() {\n    return 1;\n}\n\nfunction g() {\n    return $b;\n}\n",
        (3, 3),
        b"<?php\nfunction f() {\n    return $a + 1;\n}\n\nfunction g() {\n    return $b;\n}\n",
        b"<?php\nfunction f() {\n    return strlen();\n}\n\nfunction g() {\n    return $b;\n}\n",
    ),
    # Clearing the errcheck turns one line into three, shifting the ineffassign down.
    _Case(
        "golangci_lint",
        "a.go",
        "errcheck",
        b'package pkg\n\nimport "os"\n\nfunc Open() {\n\tos.Mkdir("x", 0o700)\n}\n\n'
        b"func Dead() int {\n\ty := 1\n\ty = 2\n\treturn y\n}\n",
        b'package pkg\n\nimport "os"\n\nfunc Open() {\n'
        b'\tif err := os.Mkdir("x", 0o700); err != nil {\n'
        b"\t\treturn\n\t}\n}\n\nfunc Dead() int {\n\ty := 1\n\ty = 2\n\treturn y\n}\n",
        (6, 8),
        b'package pkg\n\nimport "os"\n\nfunc Open() {\n\tos.Mkdir("x", 0o755)\n}\n\n'
        b"func Dead() int {\n\ty := 1\n\ty = 2\n\treturn y\n}\n",
        b'package pkg\n\nimport "os"\n\nfunc Open() {\n\t_ = os.Mkdir("x", 0o700)\n}\n\n'
        b"func Dead() int {\n\ty := 1\n\ty = 2\n\tz := 1\n\tz = 2\n\t_ = z\n\treturn y\n}\n",
    ),
]
_IDS = [c.analyzer for c in _CASES]


async def _rerun(
    rerunner: PatchReanalyzer,
    case: _Case,
    patched: bytes,
    span: tuple[int, int] = (1, 1),
) -> str:
    return await rerunner.rerun(
        case.analyzer,
        case.path,
        case.original,
        patched,
        rule_id=case.rule_id,
        patched_span=span,
        **_JOB,
    )


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("case", _CASES, ids=_IDS)
async def test_us_af6_a_fix_that_clears_the_finding_passes(case: _Case) -> None:
    # For ruff, eslint and golangci-lint the fix also shifts a later, unrelated
    # finding to a new line: line-shift safe because only counts are compared.
    result = await _rerun(_rerunner(), case, case.cleared, case.cleared_span)
    assert result == ReanalysisResult.PASSED


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("case", _CASES, ids=_IDS)
async def test_us_af6_a_fix_that_leaves_the_finding_fails(case: _Case) -> None:
    result = await _rerun(_rerunner(), case, case.left)
    assert result == ReanalysisResult.REJECTED


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("case", _CASES, ids=_IDS)
async def test_us_af6_a_fix_that_introduces_another_violation_fails(case: _Case) -> None:
    result = await _rerun(_rerunner(), case, case.introduces)
    assert result == ReanalysisResult.REJECTED


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us_af6_a_finding_of_the_rule_on_the_patched_span_fails() -> None:
    # Two F401 -> one: the count drops, but the survivor sits on the patched line.
    rerunner = _rerunner()
    result = await rerunner.rerun(
        "ruff",
        "a.py",
        b"import os\nimport sys\n",
        b"import sys\n",
        rule_id="F401",
        patched_span=(1, 1),
        **_JOB,
    )
    assert result == ReanalysisResult.REJECTED


class _CountingAnalyzer:
    """Wraps a real analyzer, recording each run and how many overlapped."""

    def __init__(self, inner: StaticAnalyzer, delay: float = 0.0) -> None:
        self._inner = inner
        self._delay = delay
        self.contents: list[bytes] = []
        self.alive = 0
        self.peak = 0

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]:
        self.contents.extend(content.values())
        self.alive += 1
        self.peak = max(self.peak, self.alive)
        try:
            await asyncio.sleep(self._delay)
            return await self._inner.run(diff, content, timeout=timeout, config_files=config_files)
        finally:
            self.alive -= 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us_af6_the_before_run_executes_once_for_two_fixes_in_one_file() -> None:
    counting = _CountingAnalyzer(RuffAnalyzer(), delay=0.05)
    rerunner = _rerunner([AnalyzerRegistration("ruff", counting, matches=match_python)])
    original = b"import os\nimport sys\n\n\ndef f():\n    return 1\n"

    results = await asyncio.gather(
        *(
            rerunner.rerun(
                "ruff",
                "a.py",
                original,
                patched,
                rule_id="F401",
                patched_span=(5, 5),
                **_JOB,
            )
            for patched in (
                b"import sys\n\n\ndef f():\n    return 1\n",
                b"import os\n\n\ndef f():\n    return 1\n",
            )
        )
    )

    assert results == [ReanalysisResult.PASSED, ReanalysisResult.PASSED]
    assert counting.contents.count(original) == 1
    assert len(counting.contents) == 3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us_af6_the_shared_semaphore_bounds_concurrent_reruns() -> None:
    counting = _CountingAnalyzer(RuffAnalyzer(), delay=0.1)
    rerunner = _rerunner([AnalyzerRegistration("ruff", counting, matches=match_python)], slots=2)

    await asyncio.gather(
        *(
            rerunner.rerun(
                "ruff",
                f"f{i}.py",
                b"import os\n",
                b"",
                rule_id="F401",
                patched_span=(1, 1),
                **_JOB,
            )
            for i in range(4)
        )
    )

    assert len(counting.contents) == 8  # four before + four after
    assert counting.peak == 2


class _ConfigFetcher:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files
        self.calls = 0

    async def fetch_config_files(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        commit_sha: str,
        names: Collection[str],
        max_file_bytes: int,
        max_files: int,
    ) -> dict[str, bytes]:
        self.calls += 1
        return {n: d for n, d in self._files.items() if n in names}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us_af6_an_engine_config_in_the_repo_is_honored_and_fetched_once() -> None:
    # E711 is not in Ruff's default profile; only the repo config selects it.
    original = b"def f(x):\n    return x == None\n"
    patched = b"def f(x):\n    return x is None\n"
    fetcher = _ConfigFetcher({"pyproject.toml": b'[tool.ruff.lint]\nselect = ["E711"]\n'})

    async def run(rerunner: AnalyzerRerunner) -> list[ReanalysisResult]:
        return [
            await rerunner.rerun(
                "ruff",
                path,
                original,
                patched,
                rule_id="E711",
                patched_span=(2, 2),
                **_JOB,
            )
            for path in ("a.py", "b.py")
        ]

    assert await run(_rerunner(config_fetcher=fetcher)) == [ReanalysisResult.PASSED] * 2
    assert fetcher.calls == 1
    assert await run(_rerunner()) == [ReanalysisResult.REJECTED] * 2  # default profile


class _FailingAnalyzer:
    def __init__(self, *, hang: bool) -> None:
        self._hang = hang

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]:
        if self._hang:
            await asyncio.sleep(30)
        raise AnalyzerInvocationError(argv=["x"], returncode=2, output=b"boom")


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("hang", [False, True], ids=["crash", "timeout"])
async def test_us_af6_a_crash_or_timeout_returns_failed(hang: bool) -> None:
    rerunner = _rerunner([AnalyzerRegistration("ruff", _FailingAnalyzer(hang=hang))], timeout=0.2)

    result = await rerunner.rerun(
        "ruff", "a.py", b"x", b"y", rule_id="F401", patched_span=(1, 1), **_JOB
    )

    assert result == ReanalysisResult.FAILED


@pytest.mark.asyncio
async def test_an_unknown_analyzer_returns_failed() -> None:
    result = await _rerunner().rerun(
        "gitleaks", "a.py", b"x", b"y", rule_id="r", patched_span=(1, 1), **_JOB
    )
    assert result == ReanalysisResult.FAILED
