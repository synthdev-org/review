"""Tests for adapter.analyzers.phpstan (plan-phase-9 Phase 7): the pure
data-only config classifier and finding normalization at the unit tier, and
the real pinned ``review-phpstan`` (minimal PHP CLI + PHPStan phar) on fixtures
at the integration tier (US-A2/A3/A4/A5/A6/A9 for PHP).
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from adapter.analyzers.exceptions import AnalyzerOutputParseError
from adapter.analyzers.phpstan import (
    _DEFAULT_PROFILE_PATH,
    PHPSTAN_CONFIG_FILENAMES,
    PhpstanAnalyzer,
    PhpstanConfigChoice,
    _parse_output,
    _to_findings,
    classify_phpstan_config,
)
from findings.models import Finding
from ingestion.diff import Diff, DiffFile
from review.file_matchers import match_php
from review.static_analysis import AnalyzerRegistration, StaticAnalysisStep

_UNDEFINED_VARIABLE = b"<?php\nfunction f() {\n    return $undefined;\n}\n"
# Only "possibly undefined": reported from level 1, not at level 0.
_MAYBE_UNDEFINED = (
    b"<?php\nfunction f(bool $c) {\n    if ($c) {\n        $a = 1;\n    }\n    return $a;\n}\n"
)
# Style-only: spacing, naming, brace placement, a long line - nothing a bug-finder cares about.
_STYLE_ONLY = (
    b"<?php\nfunction Bad_Name( $a ,$b )\n{\n  $x=1;\n  return [ $a,$b, $x, "
    b"'a very long line ' . 'x' . 'more more more more more more more more more more more'];\n}\n"
)
_CLEAN = b"<?php\n\nfunction ok(array $items): int\n{\n    return count($items);\n}\n"
# Missing dependencies only: no vendor/ is ever fetched, so none of this is a bug in the change.
_UNRESOLVED_ONLY = (
    b"<?php\nuse Vendor\\Package\\Base;\n\nclass A extends Base\n{\n"
    b"    public function f(): int\n    {\n        $m = new Missing();\n"
    b"        return strlen2('a') + UNKNOWN_CONST + $m->go();\n    }\n}\n"
)


@pytest.fixture(autouse=True)
def _enable_analyzer_logger() -> None:
    # An earlier test's logging setup may have disabled this already-imported logger.
    logging.getLogger("adapter.analyzers.phpstan").disabled = False


def _run(
    content: dict[str, bytes], config_files: dict[str, bytes] | None = None
) -> Coroutine[Any, Any, list[Finding]]:
    return PhpstanAnalyzer().run(
        Diff(files=()), content, timeout=60, config_files=config_files or {}
    )


# --- unit: pinned default profile ------------------------------------------------


def test_default_profile_checksum_matches() -> None:
    actual = hashlib.sha256(_DEFAULT_PROFILE_PATH.read_bytes()).hexdigest()
    pinned = _DEFAULT_PROFILE_PATH.with_suffix(".neon.sha256").read_text().strip()
    assert actual == pinned, (
        "phpstan_default.neon changed without phpstan_default.neon.sha256 being re-pinned"
    )


# --- unit: data-only classifier (US-A4) ------------------------------------------


def _classify(files: dict[str, str]) -> PhpstanConfigChoice:
    return classify_phpstan_config({name: text.encode() for name, text in files.items()})


def test_no_config_uses_the_default_profile_silently() -> None:
    choice = _classify({})
    assert choice.config_name is None
    assert choice.outcome.source == "default"
    assert choice.outcome.skip_reason is None


def test_plain_parameters_config_is_honored_and_reports_whether_it_sets_a_level() -> None:
    with_level = _classify(
        {"phpstan.neon": "parameters:\n    level: 5\n    paths:\n        - src\n"}
    )
    assert (with_level.config_name, with_level.outcome.source) == ("phpstan.neon", "repo")
    assert with_level.sets_level is True

    without_level = _classify({"phpstan.neon": "parameters:\n\tignoreErrors:\n\t\t- '#foo#'\n"})
    assert without_level.config_name == "phpstan.neon"
    assert without_level.sets_level is False


def test_ignore_errors_entries_and_comments_are_data() -> None:
    config = (
        "# project config\nparameters:  # settings\n    level: 3\n"
        "    ignoreErrors:\n        -\n            message: '#bootstrapFiles: x#'\n"
        "            path: src/a.php\n        - identifier: foo.bar\n\n    excludePaths:\n"
        "        - vendor\n"
    )
    assert _classify({"phpstan.neon": config}).config_name == "phpstan.neon"


def test_discovery_precedence_is_neon_then_dist() -> None:
    ok = "parameters:\n    level: 1\n"
    files = {"phpstan.dist.neon": ok, "phpstan.neon.dist": ok, "phpstan.neon": ok}
    assert _classify(files).config_name == "phpstan.neon"
    del files["phpstan.neon"]
    assert _classify(files).config_name == "phpstan.neon.dist"
    del files["phpstan.neon.dist"]
    assert _classify(files).config_name == "phpstan.dist.neon"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("parameters:\n    bootstrapFiles:\n        - b.php\n", "executable_config"),
        ("parameters:\n    level: 1\n    scanFiles:\n        - a.php\n", "executable_config"),
        ("parameters:\n    scanDirectories:\n        - lib\n", "executable_config"),
        ("parameters:\n    autoload_files:\n        - a.php\n", "executable_config"),
        ("parameters:\n    autoload_directories:\n        - lib\n", "executable_config"),
        ("includes:\n    - ext.neon\nparameters:\n    level: 1\n", "executable_config"),
        ("parameters:\n    level: 1\nservices:\n    -\n        class: Foo\n", "executable_config"),
        ("parameters:\n    level: 1\nrules:\n    - Foo\n", "executable_config"),
        ("parameters:\n    level: 1\nextensions:\n    x: Foo\n", "executable_config"),
        ("parameters:\n    stubFiles:\n        - s.php\n", "executable_config"),
        ("parameters:\n    tmpDir: /elsewhere\n", "unsupported_option"),
        ("parameters:\n    parallel:\n        maximumNumberOfProcesses: 9\n", "unsupported_option"),
        ("parameters:\n    madeUpOption: true\n", "unsupported_option"),
        ("somethingElse: 1\nparameters:\n    level: 1\n", "unsupported_option"),
        ("parameters: {level: 1, bootstrapFiles: [b.php]}\n", "unsupported_option"),
        ("level: 1\n", "unsupported_option"),
        ("  level: 1\nparameters:\n    level: 1\n", "unsupported_syntax"),
        ("parameters:\n    level: 1\n  stray: 1\n", "unsupported_syntax"),
        ("parameters:\n    - level\n", "unsupported_syntax"),
        ("parameters:\n    ignoreErrors:\n        - '%env.SECRET%'\n", "env_reference"),
        ('parameters:\n    phpVersion: "%\\u0065nv.HOME%"\n', "env_reference"),
        ("", "config_invalid"),
        ("# nothing but a comment\n", "config_invalid"),
    ],
)
def test_executable_or_unsupported_config_falls_back_to_default_with_a_reason(
    text: str, reason: str
) -> None:
    choice = _classify({"phpstan.neon": text})
    assert choice.config_name is None
    assert choice.outcome.source == "default"
    assert choice.outcome.skip_reason == reason
    assert choice.skipped_path in PHPSTAN_CONFIG_FILENAMES


def test_non_utf8_config_is_invalid_not_a_crash() -> None:
    choice = classify_phpstan_config({"phpstan.neon": b"\xff\xfe"})
    assert choice.outcome.skip_reason == "config_invalid"


# --- unit: output parsing and normalization (US-A2) ------------------------------


def _message(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message": "Undefined variable: $x",
        "line": 7,
        "ignorable": True,
        "identifier": "variable.undefined",
    }
    return {**message, **overrides}


def _entry(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"errors": len(messages), "messages": list(messages)}


def test_finding_normalization_maps_identifier_severity() -> None:
    findings = _to_findings(
        "/scan/a.php",
        _entry(
            _message(),
            _message(message="Syntax error", identifier="phpstan.parse"),
            _message(message="Method A::f() has no return type", identifier="return.type"),
            _message(message="no identifier", identifier=None),
        ),
        Path("/scan"),
        {"a.php": "orig/a.php"},
    )
    assert [(f.file, f.line, f.severity, f.source, f.rule_id) for f in findings] == [
        ("orig/a.php", 7, "high", "phpstan", "variable.undefined"),
        ("orig/a.php", 7, "high", "phpstan", "phpstan.parse"),
        ("orig/a.php", 7, "medium", "phpstan", "return.type"),
        ("orig/a.php", 7, "medium", "phpstan", None),
    ]


@pytest.mark.parametrize(
    ("message", "identifier"),
    [
        ("Class Foo not found.", "class.notFound"),
        ("Class A extends unknown class Foo\\Bar.", "class.notFound"),
        ("Function strlen2 not found.", "function.notFound"),
        ("Constant UNKNOWN not found.", "constant.notFound"),
        ("Trait T not found.", "trait.notFound"),
        ("Interface I not found.", "interface.notFound"),
        # Older/other releases without an identifier are still recognised by message.
        ("Instantiated class Missing not found.", None),
        ("Function strlen2 not found.", None),
        ("Call to static method go() on an unknown class Foo.", None),
        ("Class A implements unknown interface Foo\\I.", None),
        ("Class A uses unknown trait Foo\\T.", None),
    ],
)
def test_findings_that_are_only_a_missing_dependency_are_dropped(
    message: str, identifier: str | None
) -> None:
    entry = _entry(_message(message=message, identifier=identifier))
    assert _to_findings("/scan/a.php", entry, Path("/scan"), {}) == []


def test_line_less_message_keeps_a_none_line() -> None:
    findings = _to_findings("/scan/a.php", _entry(_message(line=None)), Path("/scan"), {})
    assert findings[0].line is None


def test_dotdot_prefixed_filename_is_inside_the_scan_directory() -> None:
    findings = _to_findings("/scan/..x.php", _entry(_message()), Path("/scan"), {})
    assert findings[0].file == "..x.php"


@pytest.mark.parametrize(
    ("filename", "entry"),
    [
        ("/elsewhere/a.php", _entry(_message())),
        ("/scan/a.php", {"errors": 1}),
        ("/scan/a.php", "not a dict"),
        ("/scan/a.php", _entry({"line": 1})),
        ("/scan/a.php", _entry(_message(message=3))),
        ("/scan/a.php", {"messages": ["not a dict"]}),
    ],
)
def test_malformed_entries_raise_parse_error(filename: str, entry: Any) -> None:
    with pytest.raises(AnalyzerOutputParseError):
        _to_findings(filename, entry, Path("/scan"), {})


@pytest.mark.parametrize("raw", [b"not json", b"\xff", b"[1]", b'{"files": 3}', b""])
def test_garbage_output_raises_parse_error(raw: bytes) -> None:
    with pytest.raises(AnalyzerOutputParseError):
        _parse_output(raw)


def test_trailing_notice_after_the_report_is_tolerated() -> None:
    raw = (
        b'{"totals":{},"files":{},"errors":[]}\n\n'
        b"  Result is incomplete because of severe errors.\n"
    )
    assert _parse_output(raw)["files"] == {}


@pytest.mark.asyncio
async def test_empty_content_short_circuits_without_spawning_phpstan() -> None:
    assert await _run({}) == []


@pytest.mark.asyncio
async def test_only_vendored_files_short_circuit_without_spawning_phpstan() -> None:
    assert await _run({"vendor/autoload.php": b"<?php\n", "app/Vendor/x.php": b"<?php\n"}) == []


# --- integration: real pinned toolset --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_correctness_violations_are_reported_with_original_paths() -> None:
    findings = await _run({"pkg/a.php": _UNDEFINED_VARIABLE})
    # A syntax error makes PHPStan skip analysis of everything else, so it runs alone.
    broken = await _run({"broken.php": b"<?php\n$x = ;\n"})

    assert {(f.file, f.rule_id) for f in findings + broken} == {
        ("pkg/a.php", "variable.undefined"),
        ("broken.php", "phpstan.parse"),
    }
    assert all(f.source == "phpstan" for f in findings + broken)
    assert [(f.line, f.severity) for f in findings] == [(3, "high")]
    assert [f.severity for f in broken] == ["high"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_default_profile_ignores_style_only_violations_and_clean_code() -> None:
    assert await _run({"style.php": _STYLE_ONLY, "clean.php": _CLEAN}) == []
    seeded = await _run({"style.php": _STYLE_ONLY + b"function g() { return $nope; }\n"})
    assert [f.rule_id for f in seeded] == ["variable.undefined"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_unresolved_classes_functions_and_constants_are_not_reported() -> None:
    """US-A5: with no vendor/ directory a missing dependency is not a finding."""
    assert await _run({"a.php": _UNRESOLVED_ONLY}) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_findings_found_is_success_not_an_invocation_error() -> None:
    # Would raise AnalyzerInvocationError if PHPStan's exit 1 were not mapped to success.
    assert await _run({"a.php": _UNDEFINED_VARIABLE})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dash_prefixed_filename_is_a_path_not_a_flag() -> None:
    findings = await _run({"-x.php": _UNDEFINED_VARIABLE})
    assert [(f.file, f.rule_id) for f in findings] == [("-x.php", "variable.undefined")]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repo_config_governs_which_rules_apply() -> None:
    content = {"a.php": _MAYBE_UNDEFINED}
    # Level 0 does not check possibly-undefined variables; the default profile (level 1) does.
    config = {"phpstan.neon": b"parameters:\n    level: 0\n"}

    assert await _run(content, config) == []
    assert [f.rule_id for f in await _run(content)] == ["variable.undefined"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_honored_config_without_a_level_gets_the_default_level() -> None:
    config = {"phpstan.neon": b"parameters:\n    reportUnmatchedIgnoredErrors: false\n"}
    findings = await _run({"a.php": _UNDEFINED_VARIABLE}, config)
    assert [f.rule_id for f in findings] == ["variable.undefined"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ignore_errors_in_the_repo_config_suppress_a_finding() -> None:
    config = {
        "phpstan.neon": (
            b"parameters:\n    level: 1\n    ignoreErrors:\n        - '#Undefined variable#'\n"
        )
    }
    assert await _run({"a.php": _UNDEFINED_VARIABLE}, config) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_config_the_tool_rejects_is_rerun_once_with_the_default_profile(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Allow-listed key, but a value PHPStan cannot parse: honored, then rejected by the tool.
    config = {"phpstan.neon": b"parameters:\n    level: [\n"}

    with caplog.at_level(logging.INFO, logger="adapter.analyzers.phpstan"):
        findings = await _run({"a.php": _UNDEFINED_VARIABLE}, config)

    assert [f.rule_id for f in findings] == ["variable.undefined"]
    reasons = [r.__dict__.get("config_skip_reason") for r in caplog.records]
    assert "rejected_by_tool" in reasons


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "config",
    [
        "parameters:\n    bootstrapFiles:\n        - bootstrap.php\n",
        "parameters:\n    scanFiles:\n        - bootstrap.php\n",
        "parameters:\n    scanDirectories:\n        - .\n",
        "parameters:\n    autoload_files:\n        - bootstrap.php\n",
        "parameters:\n    autoload_directories:\n        - .\n",
        "includes:\n    - ext.neon\n",
        "services:\n    -\n        class: Bootstrap\n        tags: [phpstan.rules.rule]\n",
    ],
)
async def test_executable_config_forms_are_never_run(
    tmp_path: Path, config: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Canary: every config form that loads or runs repo PHP would create the
    marker if honored; the config is skipped and nothing runs."""
    marker = tmp_path / "marker"
    code = f"<?php\nfile_put_contents({str(marker)!r}, 'ran');\n".encode()
    content = {"bootstrap.php": code, "ext.neon": b"", "a.php": _CLEAN}

    with caplog.at_level(logging.INFO, logger="adapter.analyzers.phpstan"):
        findings = await _run(content, {"phpstan.neon": config.encode(), "ext.neon": b""})

    assert findings == []
    assert not marker.exists()
    # The reason is what proves the classifier (not PHPStan's own parser) refused it.
    reasons = [r.__dict__.get("config_skip_reason") for r in caplog.records]
    assert "executable_config" in reasons


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_vendor_autoloader_in_the_pr_is_never_required(tmp_path: Path) -> None:
    """Canary: PHPStan requires vendor/autoload.php from its working directory,
    so a PR-supplied one must be dropped before materialization."""
    marker = tmp_path / "marker"
    code = f"<?php\nfile_put_contents({str(marker)!r}, 'ran');\n".encode()
    content = {"vendor/autoload.php": code, "a.php": _UNDEFINED_VARIABLE}

    findings = await _run(content)

    assert [(f.file, f.rule_id) for f in findings] == [("a.php", "variable.undefined")]
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tmp_dir_outside_the_scan_directory_is_skipped_and_creates_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Canary: a config naming a cache/output path outside the scan directory
    must not create anything there."""
    outside = tmp_path / "outside"
    config = {"phpstan.neon": f"parameters:\n    tmpDir: {outside}\n".encode()}

    with caplog.at_level(logging.INFO, logger="adapter.analyzers.phpstan"):
        findings = await _run({"a.php": _UNDEFINED_VARIABLE}, config)

    assert [f.rule_id for f in findings] == ["variable.undefined"]
    assert [r.__dict__.get("config_skip_reason") for r in caplog.records] == ["unsupported_option"]
    assert not outside.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_default_run_writes_its_cache_only_inside_the_scan_directory(
    tmp_path: Path,
) -> None:
    before = set(Path("/tmp").glob("phpstan*"))
    await _run({"a.php": _UNDEFINED_VARIABLE})
    assert set(Path("/tmp").glob("phpstan*")) == before


@pytest.mark.integration
def test_php_is_minimal_and_has_no_network_extensions() -> None:
    """US-A9: only the extensions the phar needs; nothing that speaks a network
    protocol, parses XML or touches a database."""
    out = subprocess.run(["php", "-n", "-m"], capture_output=True, text=True, check=True).stdout
    modules = {line.strip().lower() for line in out.splitlines() if line and "[" not in line}
    assert {"phar", "tokenizer", "mbstring", "ctype", "filter"} <= modules
    assert not modules & {
        "curl",
        "openssl",
        "sockets",
        "ftp",
        "xml",
        "dom",
        "libxml",
        "simplexml",
        "pdo",
        "sqlite3",
        "mysqli",
        "zlib",
    }


@pytest.mark.integration
def test_network_stream_wrappers_are_disabled_by_the_wrapper() -> None:
    script = Path(__file__).resolve().parents[2] / "tools" / "phpstan" / "review-phpstan"
    text = script.read_text()
    assert "allow_url_fopen=0" in text and "allow_url_include=0" in text


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
async def test_php_only_pr_posts_only_phpstan_findings_on_added_lines() -> None:
    content = {
        "app.php": (
            b"<?php\nfunction old() {\n    return $legacy;\n}\n\nfunction added() {\n"
            b"    return $fresh;\n}\n"
        ),
        "main.go": b"package main\n",
    }
    # Only lines 6-8 are added; the pre-existing undefined $legacy on line 3 is untouched.
    patch = (
        "@@ -1,4 +1,8 @@\n <?php\n function old() {\n     return $legacy;\n }\n"
        "+\n+function added() {\n+    return $fresh;\n+}\n"
    )
    diff = Diff(
        files=(
            DiffFile(path="app.php", status="modified", patch=patch, mode="100644"),
            DiffFile(path="main.go", status="added", patch="@@ -0,0 +1 @@\n+package main\n"),
        )
    )
    step = StaticAnalysisStep(
        _FetchesContent(content),
        registrations=[
            AnalyzerRegistration(
                "phpstan",
                PhpstanAnalyzer(),
                matches=match_php,
                changed_lines_only=True,
                config_filenames=PHPSTAN_CONFIG_FILENAMES,
            )
        ],
        redactor=_PassthroughRedactor(),
        enabled={"phpstan": True},
        timeouts={"phpstan": 30.0},
    )

    from review.step import StepContext

    output = await step.run(_PrefilterOutput(diff=diff), StepContext(job_id="job-1"))

    assert [(f.file, f.line, f.rule_id, f.source) for f in output.findings] == [
        ("app.php", 7, "variable.undefined", "phpstan")
    ]
    json.dumps([f.model_dump() for f in output.findings])  # serializable as posted
