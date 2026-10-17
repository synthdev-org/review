"""Tests for adapter.analyzers.ruff (plan-phase-9 Phase 3): the pure
data-only config classifier and finding normalization at the unit tier, and
the real pinned ``ruff`` binary on fixtures at the integration tier
(US-A2/A3/A4/A5/A6/A9 for Python).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from adapter.analyzers.exceptions import AnalyzerOutputParseError
from adapter.analyzers.ruff import (
    _DEFAULT_PROFILE_PATH,
    RUFF_CONFIG_FILENAMES,
    RuffAnalyzer,
    RuffConfigChoice,
    _parse_output,
    _to_finding,
    classify_ruff_config,
)
from findings.models import Finding
from ingestion.diff import Diff, DiffFile
from review.file_matchers import match_python
from review.static_analysis import AnalyzerRegistration, StaticAnalysisStep

_UNDEFINED_NAME = b"def f():\n    return undefined_name\n"
_UNUSED_IMPORT = b"import os\n"
# Style-only: spacing, quotes, a long line, naming - nothing a bug-finder cares about.
_STYLE_ONLY = (
    b"import sys\n\n\ndef Bad_Name( a ,b ):\n    x=1\n    return sys.argv, a, b, x, "
    b"'a very long line ' + 'x' * 10 + 'more more more more more more more more more more more'\n"
)
_CLEAN = b"import sys\n\n\ndef ok(argv: list[str]) -> list[str]:\n    return [*argv, *sys.argv]\n"


@pytest.fixture(autouse=True)
def _enable_analyzer_logger() -> None:
    # An earlier test's logging setup may have disabled this already-imported logger.
    logging.getLogger("adapter.analyzers.ruff").disabled = False


def _run(
    content: dict[str, bytes], config_files: dict[str, bytes] | None = None
) -> Coroutine[Any, Any, list[Finding]]:
    return RuffAnalyzer().run(Diff(files=()), content, timeout=60, config_files=config_files or {})


# --- unit: pinned default profile ------------------------------------------------


def test_default_profile_checksum_matches() -> None:
    actual = hashlib.sha256(_DEFAULT_PROFILE_PATH.read_bytes()).hexdigest()
    pinned = _DEFAULT_PROFILE_PATH.with_suffix(".toml.sha256").read_text().strip()
    assert actual == pinned, (
        "ruff_default.toml changed without ruff_default.toml.sha256 being re-pinned"
    )


# --- unit: data-only classifier (US-A4) ------------------------------------------


def _classify(files: dict[str, str]) -> RuffConfigChoice:
    return classify_ruff_config({name: text.encode() for name, text in files.items()})


def test_no_config_uses_the_default_profile_silently() -> None:
    choice = _classify({})
    assert choice.config_name is None
    assert choice.outcome.source == "default"
    assert choice.outcome.skip_reason is None


def test_ruff_toml_is_honored() -> None:
    choice = _classify({"ruff.toml": '[lint]\nselect = ["E"]\n'})
    assert choice.config_name == "ruff.toml"
    assert choice.outcome.source == "repo"


def test_pyproject_with_tool_ruff_is_honored_and_without_is_ignored() -> None:
    assert _classify({"pyproject.toml": "[tool.ruff]\nline-length = 100\n"}).config_name == (
        "pyproject.toml"
    )
    ignored = _classify({"pyproject.toml": '[project]\nname = "x"\n'})
    assert ignored.config_name is None
    assert ignored.outcome.skip_reason is None


def test_ruff_discovery_precedence_is_dot_ruff_then_ruff_then_pyproject() -> None:
    files = {
        "pyproject.toml": "[tool.ruff]\n",
        "ruff.toml": "",
        ".ruff.toml": "",
    }
    assert _classify(files).config_name == ".ruff.toml"
    del files[".ruff.toml"]
    assert _classify(files).config_name == "ruff.toml"


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ({"ruff.toml": 'cache-dir = "/tmp/elsewhere"\n'}, "cache_dir_not_allowed"),
        ({"ruff.toml": 'extend = "/etc/passwd"\n'}, "extend_unresolved"),
        ({"ruff.toml": 'extend = "../other/ruff.toml"\n'}, "extend_unresolved"),
        ({"ruff.toml": 'extend = "missing.toml"\n'}, "extend_unresolved"),
        ({"ruff.toml": "extend = 3\n"}, "extend_unresolved"),
        ({"ruff.toml": 'extend = "ruff.toml"\n'}, "extend_unresolved"),
        ({"ruff.toml": "bad = [\n"}, "config_invalid"),
        (
            {
                "ruff.toml": 'extend = "pyproject.toml"\n',
                "pyproject.toml": "[tool.ruff]\ncache-dir = 'x'\n",
            },
            "cache_dir_not_allowed",
        ),
        (
            {"ruff.toml": 'extend = ".ruff.toml"\n', ".ruff.toml": 'extend = "ruff.toml"\n'},
            "extend_unresolved",
        ),
    ],
)
def test_unsafe_or_unresolved_config_falls_back_to_default_with_a_reason(
    files: dict[str, str], reason: str
) -> None:
    choice = _classify(files)
    assert choice.config_name is None
    assert choice.outcome.source == "default"
    assert choice.outcome.skip_reason == reason
    assert choice.skipped_path in RUFF_CONFIG_FILENAMES


def test_extend_to_a_fetched_root_file_is_honored() -> None:
    choice = _classify(
        {"ruff.toml": 'extend = "./pyproject.toml"\n', "pyproject.toml": "[tool.ruff]\n"}
    )
    assert choice.config_name == "ruff.toml"


# --- unit: output parsing and normalization (US-A2) ------------------------------


def _entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "code": "F821",
        "filename": "/scan/a.py",
        "location": {"row": 7, "column": 1},
        "message": "Undefined name `x`",
        "severity": "error",
    }
    return {**entry, **overrides}


def test_finding_normalization_maps_rule_and_ruff_severity() -> None:
    f = _to_finding(_entry(), Path("/scan"), {"a.py": "orig/a.py"})
    assert (f.file, f.line, f.severity, f.source, f.rule_id) == (
        "orig/a.py",
        7,
        "high",
        "ruff",
        "F821",
    )
    assert _to_finding(_entry(code="F401"), Path("/scan"), {}).severity == "low"
    assert _to_finding(_entry(code=None), Path("/scan"), {}).rule_id is None
    # Not a pinned rule: Ruff's own severity decides; unmapped defaults to medium.
    assert _to_finding(_entry(code="E711"), Path("/scan"), {}).severity == "medium"
    assert _to_finding(_entry(code="E711", severity="warning"), Path("/scan"), {}).severity == "low"
    assert (
        _to_finding(_entry(code="E711", severity="weird"), Path("/scan"), {}).severity == "medium"
    )
    assert _to_finding(_entry(code="E711", severity=None), Path("/scan"), {}).severity == "medium"


def test_dotdot_prefixed_filename_is_inside_the_scan_directory() -> None:
    f = _to_finding(_entry(filename="/scan/..x.py"), Path("/scan"), {})
    assert f.file == "..x.py"


@pytest.mark.parametrize(
    "entry",
    [{"code": "F1"}, _entry(filename="/elsewhere/a.py"), _entry(message=3), _entry(filename=None)],
)
def test_malformed_entries_raise_parse_error(entry: dict[str, Any]) -> None:
    with pytest.raises(AnalyzerOutputParseError):
        _to_finding(entry, Path("/scan"), {})


@pytest.mark.parametrize("raw", [b"not json", b"\xff", b'{"a": 1}'])
def test_garbage_output_raises_parse_error(raw: bytes) -> None:
    with pytest.raises(AnalyzerOutputParseError):
        _parse_output(raw)


@pytest.mark.asyncio
async def test_empty_content_short_circuits_without_spawning_ruff() -> None:
    assert await _run({}) == []


# --- integration: real pinned binary --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_correctness_violations_are_reported_with_original_paths() -> None:
    findings = await _run(
        {"pkg/a.py": _UNDEFINED_NAME, "b.py": _UNUSED_IMPORT, "broken.py": b"def f(:\n  pass\n"}
    )

    by_rule = {(f.file, f.rule_id) for f in findings}
    assert ("pkg/a.py", "F821") in by_rule
    assert ("b.py", "F401") in by_rule
    assert ("broken.py", "invalid-syntax") in by_rule
    assert all(f.source == "ruff" for f in findings)
    undefined = next(f for f in findings if f.rule_id == "F821")
    assert (undefined.line, undefined.severity) == (2, "high")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_default_profile_ignores_style_only_violations_and_clean_code() -> None:
    assert await _run({"style.py": _STYLE_ONLY, "clean.py": _CLEAN}) == []
    seeded = await _run({"style.py": _STYLE_ONLY + b"\nprint(undefined_name)\n"})
    assert [f.rule_id for f in seeded] == ["F821"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_findings_found_is_success_not_an_invocation_error() -> None:
    # Would raise AnalyzerInvocationError if Ruff's exit 1 were not mapped to success.
    assert await _run({"a.py": _UNUSED_IMPORT})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dash_prefixed_filename_is_a_path_not_a_flag() -> None:
    findings = await _run({"-x.py": _UNUSED_IMPORT})
    assert [(f.file, f.rule_id) for f in findings] == [("-x.py", "F401")]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repo_config_governs_which_rules_apply() -> None:
    content = {"a.py": b"import os\n\n\ndef f(x):\n    return x == None\n"}
    config = {"pyproject.toml": b'[tool.ruff.lint]\nselect = ["E711"]\n'}

    findings = await _run(content, config)

    assert [f.rule_id for f in findings] == ["E711"]  # honored: no F401 from the default profile
    assert {f.rule_id for f in await _run(content)} == {"F401"}  # default profile


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ruff_toml_extending_a_fetched_root_file_resolves() -> None:
    content = {"a.py": b"def f(x):\n    return x == None\n"}
    config = {
        "ruff.toml": b'extend = "pyproject.toml"\n',
        "pyproject.toml": b'[tool.ruff.lint]\nselect = ["E711"]\n',
    }
    assert [f.rule_id for f in await _run(content, config)] == ["E711"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_config_the_tool_rejects_is_rerun_once_with_the_default_profile() -> None:
    # Valid TOML the classifier accepts, but Ruff rejects (unknown rule code).
    config = {"ruff.toml": b'[lint]\nselect = ["NOT-A-RULE"]\n'}

    findings = await _run({"a.py": _UNUSED_IMPORT}, config)

    assert [f.rule_id for f in findings] == ["F401"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cache_dir_outside_the_scan_directory_is_skipped_and_creates_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Canary: ``--no-cache`` alone would also keep the directory empty, so the
    guard is proven by the logged skip reason, not by the empty directory."""
    outside = tmp_path / "outside"
    config = {"ruff.toml": f'cache-dir = "{outside}/cache"\n'.encode()}

    with caplog.at_level(logging.INFO, logger="adapter.analyzers.ruff"):
        findings = await _run({"a.py": _UNUSED_IMPORT}, config)

    assert [f.rule_id for f in findings] == ["F401"]
    skips = [r.__dict__.get("config_skip_reason") for r in caplog.records]
    assert skips == ["cache_dir_not_allowed"]
    assert not outside.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_extend_outside_the_scan_directory_is_skipped_not_read(tmp_path: Path) -> None:
    """Canary: the outside file exists and would add E711 if it were read."""
    outside = tmp_path / "outside"
    outside.mkdir()
    evil = outside / "evil.toml"
    evil.write_text('[lint]\nselect = ["E711"]\n')
    config = {"pyproject.toml": f'[tool.ruff]\nextend = "{evil}"\n'.encode()}

    findings = await _run({"a.py": b"import os\nx = 1\nprint(x == None)\n"}, config)

    assert sorted(f.rule_id or "" for f in findings) == ["F401"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_executable_forms_in_the_repo_are_never_run(tmp_path: Path) -> None:
    """Canary: Ruff's config is pure TOML so it has no executable config form;
    this proves that scanning repo code that would write a marker when
    executed (a module, a setup script, a build backend named in pyproject)
    never runs any of it."""
    marker = tmp_path / "marker"
    code = f"open({str(marker)!r}, 'w').write('ran')\n".encode()
    content = {"setup.py": code, "conftest.py": code, "plugin.py": code}
    config = {
        "pyproject.toml": (
            b'[build-system]\nbuild-backend = "plugin"\nbackend-path = ["."]\n'
            b'[tool.ruff]\nsrc = ["."]\n'
        )
    }

    await _run(content, config)

    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_extend_target_outside_fetched_set_is_not_read(tmp_path: Path) -> None:
    secret = tmp_path / "secret.toml"
    secret.write_text('[lint]\nselect = ["E711"]\n')
    config = {"ruff.toml": f'extend = "{secret}"\n'.encode()}

    findings = await _run({"a.py": _UNUSED_IMPORT}, config)

    assert [f.rule_id for f in findings] == ["F401"]  # default profile, not the outside file


# --- integration: through the real step (US-A1, US-A3) ----------------------------


@dataclass
class _PrefilterOutput:
    diff: Diff
    org_id: str = "org-1"
    repo_id: str = "repo-1"
    installation_id: int = 1
    repo_full_name: str = "acme/widgets"
    pr_number: int = 42
    head_sha: str = "deadbeef"
    base_sha: str | None = None
    pr_title: str | None = None
    pr_head_ref: str | None = None
    pr_issue_keys: tuple[str, ...] = ()
    pr_is_fork: bool | None = None
    private: bool = False


class _FetchesContent:
    """A ``DiffFetcher`` fake: ``StaticAnalysisStep`` only calls ``fetch_content``."""

    def __init__(self, content: dict[str, bytes]) -> None:
        self._content = content

    async def fetch(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> Diff:
        raise NotImplementedError("StaticAnalysisStep never calls DiffFetcher.fetch()")

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        diff: Diff,
        paths: Sequence[str],
    ) -> dict[str, bytes]:
        return dict(self._content)


class _PassthroughRedactor:
    async def redact(self, text: str) -> str:
        return text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_python_only_pr_posts_only_ruff_findings_on_added_lines() -> None:
    content = {
        "app.py": b"import os\n\n\ndef f():\n    return undefined_name\n",
        "main.go": b"package main\n",
    }
    # Only line 5 (the undefined name) is added; the unused import on line 1 is untouched.
    patch = "@@ -1,4 +1,5 @@\n import os\n \n \n def f():\n+    return undefined_name\n"
    diff = Diff(
        files=(
            DiffFile(path="app.py", status="modified", patch=patch, mode="100644"),
            DiffFile(path="main.go", status="added", patch="@@ -0,0 +1 @@\n+package main\n"),
        )
    )
    step = StaticAnalysisStep(
        _FetchesContent(content),
        registrations=[
            AnalyzerRegistration(
                "ruff",
                RuffAnalyzer(),
                matches=match_python,
                changed_lines_only=True,
                config_filenames=RUFF_CONFIG_FILENAMES,
            )
        ],
        redactor=_PassthroughRedactor(),
        enabled={"ruff": True},
        timeouts={"ruff": 30.0},
    )

    from review.step import StepContext

    output = await step.run(_PrefilterOutput(diff=diff), StepContext(job_id="job-1"))

    assert [(f.file, f.line, f.rule_id, f.source) for f in output.findings] == [
        ("app.py", 5, "F821", "ruff")
    ]
    json.dumps([f.model_dump() for f in output.findings])  # serializable as posted


def test_pathologically_nested_config_falls_back_to_default() -> None:
    choice = classify_ruff_config({"ruff.toml": b"a = " + b"[" * 100_000 + b"]" * 100_000})
    assert choice.config_name is None
    assert choice.outcome.skip_reason == "config_invalid"
