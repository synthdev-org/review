"""Tests for adapter.analyzers.golangci_lint (plan-phase-9 Phase 5): the pure
data-only config classifier and finding normalization at the unit tier, and
the real pinned ``golangci-lint`` + Go toolchain on fixtures at the integration
tier (US-A2/A3/A4/A5/A6/A9 for Go).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from adapter.analyzers.exceptions import AnalyzerOutputParseError
from adapter.analyzers.golangci_lint import (
    _DEFAULT_PROFILE_PATH,
    GOLANGCI_CONFIG_FILENAMES,
    GolangciConfigChoice,
    GolangciLintAnalyzer,
    _parse_report,
    _scratch_env,
    _to_findings,
    classify_golangci_config,
)
from findings.models import Finding
from ingestion.diff import Diff, DiffFile
from review.file_matchers import match_go
from review.static_analysis import AnalyzerRegistration, StaticAnalysisStep

# An ignored error return (errcheck), a Printf verb/argument mismatch (govet) and a
# dead assignment (ineffassign): all correctness, none style.
_CORRECTNESS = b"""package pkg

import (
	"fmt"
	"os"
)

func Open() {
	os.Mkdir("x", 0o700)
}

func Print() {
	fmt.Printf("%d\\n", "s")
}

func Dead() int {
	y := 1
	y = 2
	return y
}
"""
# staticcheck SA5000: assignment to a nil map.
_STATICCHECK = b"""package pkg

func Fill() {
	var m map[string]int
	m["a"] = 1
}
"""
# Style only: an underscore name, a capitalised error string, a missing doc comment,
# a long line, a redundant else.
_STYLE_ONLY = b"""package pkg

import "errors"

func Bad_name(a int) error {
	if a > 1 {
		return errors.New("Bad Thing.")
	} else {
		return nil
	}
}
"""
_CLEAN = b"""package pkg

import (
	"errors"
	"fmt"
)

// Parse returns the value or an error.
func Parse(s string) (int, error) {
	if s == "" {
		return 0, errors.New("empty")
	}
	var n int
	if _, err := fmt.Sscanf(s, "%d", &n); err != nil {
		return 0, fmt.Errorf("parse %q: %w", s, err)
	}
	return n, nil
}

// Map applies f to every element (generics need a modern language version).
func Map[T, U any](in []T, f func(T) U) []U {
	out := make([]U, 0, len(in))
	for _, v := range in {
		out = append(out, f(v))
	}
	return out
}
"""
# Only a missing third party module, and an identifier a sibling file would define.
_UNRESOLVED_ONLY = b"""package pkg

import "github.com/acme/thing"

func Run() {
	thing.Do()
	helper()
}
"""
# A real compile error that needs nothing outside the file.
_COMPILE_ERROR = b"""package pkg

func Unused() {
	x := 1
}
"""


@pytest.fixture(autouse=True)
def _enable_analyzer_logger() -> None:
    # An earlier test's logging setup may have disabled this already-imported logger.
    logging.getLogger("adapter.analyzers.golangci_lint").disabled = False


def _run(
    content: dict[str, bytes], config_files: dict[str, bytes] | None = None
) -> Coroutine[Any, Any, list[Finding]]:
    return GolangciLintAnalyzer().run(
        Diff(files=()), content, timeout=120, config_files=config_files or {}
    )


# --- unit: pinned default profile ------------------------------------------------


def test_default_profile_checksum_matches() -> None:
    actual = hashlib.sha256(_DEFAULT_PROFILE_PATH.read_bytes()).hexdigest()
    pinned = _DEFAULT_PROFILE_PATH.with_suffix(".yml.sha256").read_text().strip()
    assert actual == pinned, (
        "golangci_lint_default.yml changed without golangci_lint_default.yml.sha256 being re-pinned"
    )


# --- unit: data-only classifier (US-A4) ------------------------------------------


def _classify(files: dict[str, str]) -> GolangciConfigChoice:
    return classify_golangci_config({name: text.encode() for name, text in files.items()})


_GOOD_YAML = 'version: "2"\nlinters:\n  default: none\n  enable: [govet]\n'


def test_no_config_uses_the_default_profile_silently() -> None:
    choice = _classify({})
    assert choice.config_name is None
    assert choice.outcome.source == "default"
    assert choice.outcome.skip_reason is None


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (".golangci.yml", _GOOD_YAML),
        (".golangci.yaml", _GOOD_YAML),
        (".golangci.toml", 'version = "2"\n[linters]\ndefault = "none"\nenable = ["govet"]\n'),
        (
            ".golangci.json",
            '{"version": "2", "linters": {"default": "none", "enable": ["govet"]}}',
        ),
    ],
)
def test_every_data_config_form_is_honored(name: str, text: str) -> None:
    choice = _classify({name: text})
    assert (choice.config_name, choice.outcome.source) == (name, "repo")
    assert choice.outcome.skip_reason is None


def test_discovery_precedence_follows_golangci_lint() -> None:
    files = dict.fromkeys(reversed(GOLANGCI_CONFIG_FILENAMES), _GOOD_YAML)
    # TOML/JSON bodies are not YAML-valid for every key, so only the first two matter here.
    assert _classify({".golangci.yaml": _GOOD_YAML, ".golangci.yml": _GOOD_YAML}).config_name == (
        ".golangci.yml"
    )
    assert _classify(files).config_name == ".golangci.yml"


def test_only_the_first_existing_file_is_considered() -> None:
    # golangci-lint would use .golangci.yml; the unsafe .yaml behind it is irrelevant.
    unsafe = 'version: "2"\noutput:\n  formats:\n    text:\n      path: /etc/x\n'
    assert _classify({".golangci.yml": _GOOD_YAML, ".golangci.yaml": unsafe}).config_name == (
        ".golangci.yml"
    )
    assert _classify({".golangci.yml": unsafe}).outcome.skip_reason == "output_path_not_allowed"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        # Not a v2 config at all.
        ("linters:\n  enable: [govet]\n", "unsupported_config_version"),
        ('version: "1"\n', "unsupported_config_version"),
        # Code loaded or run from the repo.
        (
            'version: "2"\nlinters:\n  settings:\n    custom:\n      evil:\n'
            "        type: goplugin\n"
            "        path: ./evil.so\n",
            "executable_config",
        ),
        (
            'version: "2"\nlinters:\n  settings:\n    custom:\n      evil:\n        type: module\n',
            "executable_config",
        ),
        (
            'version: "2"\nlinters:\n  enable: [gocritic]\n  settings:\n    gocritic:\n'
            "      settings:\n        ruleguard:\n          rules: rules.go\n",
            "executable_config",
        ),
        # A path the PR names, or git.
        (
            'version: "2"\nlinters:\n  settings:\n    goheader:\n'
            "      template-path: /etc/passwd\n",
            "unsupported_option",
        ),
        ('version: "2"\nissues:\n  new-from-rev: HEAD~1\n', "unsupported_option"),
        ('version: "2"\nissues:\n  new-from-patch: /tmp/x.patch\n', "unsupported_option"),
        # Cache and output locations outside the scan directory.
        ('version: "2"\nrun:\n  cache-dir: /var/cache\n', "cache_dir_not_allowed"),
        ('version: "2"\nrun:\n  go-build-cache: /x\n', "cache_dir_not_allowed"),
        (
            'version: "2"\noutput:\n  formats:\n    checkstyle:\n      path: /tmp/out.xml\n',
            "output_path_not_allowed",
        ),
        (
            'version: "2"\noutput:\n  formats:\n    json:\n      path: report.json\n',
            "output_path_not_allowed",
        ),
        ('version: "2"\noutput:\n  formats: [text]\n', "unsupported_syntax"),
        # golangci-lint reads keys case-insensitively; capitalization is no bypass.
        (
            'version: "2"\nOutput:\n  Formats:\n    Text:\n      Path: /scan/pwned.txt\n',
            "output_path_not_allowed",
        ),
        (
            'version: "2"\nLinters:\n  Settings:\n    Custom:\n      x:\n        type: module\n',
            "executable_config",
        ),
        ('version: "2"\nIssues:\n  New-From-Rev: HEAD~1\n', "unsupported_option"),
        ('version: "2"\nRun:\n  Cache-Dir: /x\n', "cache_dir_not_allowed"),
        # Unparseable.
        ('version: "2"\nlinters: [\n', "config_invalid"),
        ("", "config_invalid"),
        ("- a\n- b\n", "config_invalid"),
    ],
)
def test_executable_or_unsafe_config_falls_back_to_default_with_a_reason(
    text: str, reason: str
) -> None:
    choice = _classify({".golangci.yml": text})
    assert choice.config_name is None
    assert choice.outcome.source == "default"
    assert choice.outcome.skip_reason == reason
    assert choice.skipped_path == ".golangci.yml"


@pytest.mark.parametrize("path", ["stdout", "stderr", ""])
def test_output_to_stdout_or_stderr_is_harmless_and_honored(path: str) -> None:
    text = f'version: "2"\noutput:\n  formats:\n    text:\n      path: "{path}"\n'
    assert _classify({".golangci.yml": text}).config_name == ".golangci.yml"


def test_ordinary_linter_settings_are_data() -> None:
    text = (
        'version: "2"\nlinters:\n  default: standard\n  enable: [revive]\n  settings:\n'
        "    errcheck:\n      check-blank: true\n    revive:\n      rules:\n"
        "        - name: exported\n"
        "  exclusions:\n    paths: [vendor]\nrun:\n  tests: false\n  timeout: 5m\n"
    )
    assert _classify({".golangci.yml": text}).config_name == ".golangci.yml"


def test_non_utf8_and_malformed_documents_are_invalid_not_a_crash() -> None:
    assert classify_golangci_config({".golangci.yml": b"\xff\xfe"}).outcome.skip_reason == (
        "config_invalid"
    )
    assert classify_golangci_config({".golangci.json": b"{"}).outcome.skip_reason == (
        "config_invalid"
    )
    assert classify_golangci_config({".golangci.toml": b"= ="}).outcome.skip_reason == (
        "config_invalid"
    )


def test_pathologically_nested_config_is_invalid_not_a_crash() -> None:
    deep = b'{"version": "2", "a": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    assert classify_golangci_config({".golangci.json": deep}).outcome.skip_reason == (
        "config_invalid"
    )


# --- unit: invocation hardening (US-A9) ------------------------------------------


def test_scratch_env_is_offline_and_keeps_every_cache_inside_the_scratch_directory() -> None:
    scratch = Path("/scan/.review-golangci")
    env = _scratch_env(scratch)

    assert env["GOPROXY"] == "off"
    assert env["GOTOOLCHAIN"] == "local"
    assert env["GOSUMDB"] == "off"
    assert env["GOWORK"] == "off"
    assert env["CGO_ENABLED"] == "0"
    assert "-mod=mod" in env["GOFLAGS"]
    for name in (
        "GOCACHE",
        "GOMODCACHE",
        "GOPATH",
        "GOTMPDIR",
        "TMPDIR",
        "HOME",
        "XDG_CACHE_HOME",
        "GOLANGCI_LINT_CACHE",
    ):
        assert Path(env[name]).is_relative_to(scratch), name


# --- unit: output parsing and normalization (US-A2) ------------------------------


def _issue(**overrides: Any) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "FromLinter": "errcheck",
        "Text": "Error return value is not checked",
        "Pos": {"Filename": "a.go", "Offset": 10, "Line": 7, "Column": 2},
    }
    return {**issue, **overrides}


def _normalize(entry: dict[str, Any], mapping: dict[str, str] | None = None) -> list[Finding]:
    return _to_findings(entry, Path("/scan"), mapping or {})


def test_finding_normalization_maps_linter_to_severity_rule_and_original_path() -> None:
    findings = [
        f
        for entry in (
            _issue(),
            _issue(FromLinter="govet", Text="printf: wrong type"),
            _issue(FromLinter="ineffassign", Text="ineffectual assignment to y"),
            _issue(FromLinter="staticcheck", Text="SA5000: assignment to nil map"),
            _issue(FromLinter="revive", Text="exported thing should have comment"),
            _issue(FromLinter="typecheck", Text="declared and not used: x"),
        )
        for f in _normalize(entry, {"a.go": "orig/a.go"})
    ]

    assert [(f.file, f.line, f.severity, f.source, f.rule_id) for f in findings] == [
        ("orig/a.go", 7, "medium", "golangci_lint", "errcheck"),
        ("orig/a.go", 7, "medium", "golangci_lint", "govet"),
        ("orig/a.go", 7, "low", "golangci_lint", "ineffassign"),
        ("orig/a.go", 7, "medium", "golangci_lint", "SA5000"),
        ("orig/a.go", 7, "medium", "golangci_lint", "revive"),
        ("orig/a.go", 7, "high", "golangci_lint", "typecheck"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "could not import github.com/acme/thing (main.go:7:2: cannot find module providing"
        " package github.com/acme/thing: module lookup disabled by GOPROXY=off)",
        "could not import example.com/other/missing (x)",
        "undefined: helper",
        "undefined: thing.Do",
        "h.Method undefined (type Helper has no field or method Method)",
        "cannot find module providing package github.com/acme/thing",
        "no required module provides package github.com/acme/thing; to add it:",
    ],
)
def test_typecheck_findings_caused_by_a_missing_dependency_or_sibling_are_dropped(
    text: str,
) -> None:
    assert _normalize(_issue(FromLinter="typecheck", Text=text)) == []


def test_a_stdlib_import_that_cannot_load_stays_visible() -> None:
    """A broken toolchain must not be mistaken for an unresolved third-party import."""
    findings = _normalize(
        _issue(FromLinter="typecheck", Text="could not import fmt (/usr/local/go/src/fmt: x)")
    )
    assert [f.rule_id for f in findings] == ["typecheck"]


def test_unresolved_text_only_matters_for_the_typecheck_linter() -> None:
    findings = _normalize(_issue(FromLinter="revive", Text="undefined: is just a word here"))
    assert [f.rule_id for f in findings] == ["revive"]


def test_package_compile_errors_are_split_onto_their_own_lines() -> None:
    text = (
        ": # review.local/scan/pkg\n./pkg/a.go:11:2: declared and not used: y\n"
        './pkg/a.go:14:6: undefined: helper\n./pkg/b.go:3:1: imported and not used: "os"\n'
    )
    findings = _normalize(
        _issue(FromLinter="typecheck", Text=text, Pos={"Filename": "", "Line": 1, "Column": 0}),
        {"pkg/a.go": "orig/pkg/a.go"},
    )

    assert [(f.file, f.line, f.message) for f in findings] == [
        ("orig/pkg/a.go", 11, "declared and not used: y"),
        ("pkg/b.go", 3, 'imported and not used: "os"'),
    ]


def test_line_less_issue_keeps_a_none_line() -> None:
    assert _normalize(_issue(Pos={"Filename": "a.go", "Line": 0}))[0].line is None


def test_dotdot_prefixed_filename_is_inside_the_scan_directory() -> None:
    assert _normalize(_issue(Pos={"Filename": "..x.go", "Line": 2}))[0].file == "..x.go"


@pytest.mark.parametrize(
    "entry",
    [
        _issue(Pos={"Filename": "../elsewhere/a.go", "Line": 1}),
        _issue(Pos={"Filename": "/elsewhere/a.go", "Line": 1}),
        {"FromLinter": "errcheck", "Text": "x"},
        _issue(Text=3),
        _issue(Pos="a.go"),
        _issue(FromLinter=None),
    ],
)
def test_malformed_or_escaping_entries_raise_parse_error(entry: dict[str, Any]) -> None:
    with pytest.raises(AnalyzerOutputParseError):
        _normalize(entry)


@pytest.mark.parametrize("raw", [b"not json", b"\xff", b"[1]", b'{"Issues": 3}', b""])
def test_garbage_report_raises_parse_error(raw: bytes) -> None:
    with pytest.raises(AnalyzerOutputParseError):
        _parse_report(raw)


def test_a_report_without_issues_is_empty() -> None:
    assert _parse_report(b'{"Issues": null, "Report": {}}') == []
    assert _parse_report(b'{"Report": {}}') == []


@pytest.mark.asyncio
async def test_empty_content_short_circuits_without_spawning_golangci_lint() -> None:
    assert await _run({}) == []


# --- integration: real pinned toolchain --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_correctness_violations_are_reported_with_original_paths() -> None:
    findings = await _run({"pkg/a.go": _CORRECTNESS, "pkg/b.go": _STATICCHECK})

    assert {(f.file, f.rule_id) for f in findings} == {
        ("pkg/a.go", "errcheck"),
        ("pkg/a.go", "govet"),
        ("pkg/a.go", "ineffassign"),
        ("pkg/b.go", "SA5000"),
    }
    assert all(f.source == "golangci_lint" and f.line for f in findings)
    by_rule = {f.rule_id: f for f in findings}
    assert (by_rule["errcheck"].line, by_rule["govet"].line) == (9, 13)
    assert by_rule["ineffassign"].line == 17


@pytest.mark.asyncio
@pytest.mark.integration
async def test_default_profile_ignores_style_only_violations_and_clean_code() -> None:
    assert await _run({"style.go": _STYLE_ONLY, "clean.go": _CLEAN}) == []
    seeded = await _run({"style.go": _STYLE_ONLY, "bug.go": _STATICCHECK})
    assert [(f.file, f.rule_id) for f in seeded] == [("bug.go", "SA5000")]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_unresolved_imports_and_sibling_symbols_are_not_reported() -> None:
    """US-A5: with only the changed files present, a missing dependency is not a finding."""
    assert await _run({"a.go": _UNRESOLVED_ONLY}) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_real_compile_error_is_still_reported() -> None:
    findings = await _run({"pkg/a.go": _COMPILE_ERROR})
    assert [(f.file, f.line, f.rule_id, f.severity) for f in findings] == [
        ("pkg/a.go", 4, "typecheck", "high")
    ]
    assert "declared and not used" in findings[0].message


@pytest.mark.asyncio
@pytest.mark.integration
async def test_findings_found_is_success_not_an_invocation_error() -> None:
    # Would raise AnalyzerInvocationError if golangci-lint exited non-zero on findings.
    assert await _run({"a.go": _CORRECTNESS})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dash_prefixed_filename_is_not_a_flag_and_does_not_break_its_package() -> None:
    """The go command rejects `-x.go` outright; it must neither reach argv as a
    flag nor turn its sibling's lint into a package-level load error."""
    assert await _run({"-x.go": _STATICCHECK}) == []
    findings = await _run({"pkg/-x.go": _STATICCHECK, "pkg/ok.go": _STATICCHECK})
    assert [(f.file, f.rule_id) for f in findings] == [("pkg/ok.go", "SA5000")]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pr_paths_under_the_scratch_prefix_cannot_break_the_run() -> None:
    """A PR file at `.review-golangci/gocache/x.go` must not pre-create our scratch
    directories (FileExistsError); it is simply not linted."""
    findings = await _run({".review-golangci/gocache/x.go": _STATICCHECK, "ok.go": _STATICCHECK})
    assert [f.file for f in findings] == ["ok.go"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_files_in_directories_go_ignores_yield_nothing_rather_than_failing() -> None:
    assert await _run({"testdata/a.go": _STATICCHECK}) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_pr_supplied_go_mod_cannot_replace_the_generated_one() -> None:
    """A go.mod demanding a newer toolchain would otherwise fail every run."""
    content = {
        "go.mod": b"module evil\n\ngo 1.99\ntoolchain go1.99.9\n",
        "a.go": _STATICCHECK,
    }
    assert [f.rule_id for f in await _run(content)] == ["SA5000"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_concurrent_runs_do_not_share_state_or_a_lock() -> None:
    results = await asyncio.gather(*(_run({"a.go": _STATICCHECK}) for _ in range(3)))
    assert [[f.rule_id for f in r] for r in results] == [["SA5000"]] * 3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repo_config_governs_which_rules_apply() -> None:
    content = {"a.go": _CORRECTNESS}
    only_govet = {".golangci.yml": _GOOD_YAML.encode()}

    assert {f.rule_id for f in await _run(content, only_govet)} == {"govet"}
    assert {f.rule_id for f in await _run(content)} == {"errcheck", "govet", "ineffassign"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repo_config_can_enable_linters_beyond_the_default() -> None:
    config = {".golangci.yml": b'version: "2"\nlinters:\n  default: none\n  enable: [misspell]\n'}
    content = {"a.go": b"package pkg\n\n// Recieve is misspelled.\nfunc Recieve() {}\n"}

    assert {f.rule_id for f in await _run(content, config)} == {"misspell"}
    assert await _run(content) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_config_the_tool_rejects_is_rerun_once_with_the_default_profile(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Plain data to the classifier, but golangci-lint does not know this linter.
    config = {
        ".golangci.yml": b'version: "2"\nlinters:\n  default: none\n  enable: [no-such-linter]\n'
    }

    with caplog.at_level(logging.INFO, logger="adapter.analyzers.golangci_lint"):
        findings = await _run({"a.go": _STATICCHECK}, config)

    assert [f.rule_id for f in findings] == ["SA5000"]
    reasons = [r.__dict__.get("config_skip_reason") for r in caplog.records]
    assert "rejected_by_tool" in reasons


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "config",
    [
        'version: "2"\nlinters:\n  settings:\n    custom:\n      evil:\n        type: goplugin\n'
        "        path: ./evil.so\n",
        'version: "2"\nlinters:\n  default: none\n  enable: [gocritic]\n  settings:\n'
        "    gocritic:\n      enabled-checks: [ruleguard]\n      settings:\n        ruleguard:\n"
        "          rules: rules.go\n",
    ],
)
async def test_executable_config_forms_are_never_run(
    tmp_path: Path, config: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Canary: every config form that loads or runs repo code would create the
    marker if honored (a Go rules file / plugin the PR itself supplies); the
    config is skipped and nothing runs."""
    marker = tmp_path / "marker"
    rules = (
        'package gorules\n\nimport "os"\n\nfunc init() {\n'
        f'\t_ = os.WriteFile("{marker}", []byte("ran"), 0o600)\n}}\n'
    ).encode()
    content = {"gorules/rules.go": rules, "a.go": _CLEAN}

    with caplog.at_level(logging.INFO, logger="adapter.analyzers.golangci_lint"):
        findings = await _run(content, {".golangci.yml": config.encode()})

    assert findings == []
    assert not marker.exists()
    # The reason is what proves the classifier (not golangci-lint's own parser) refused it.
    reasons = [r.__dict__.get("config_skip_reason") for r in caplog.records]
    assert "executable_config" in reasons


@pytest.mark.asyncio
@pytest.mark.integration
async def test_output_path_outside_the_scan_directory_is_skipped_and_creates_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Canary: a config naming an output path outside the scan directory must
    not create anything there (golangci-lint would write it, as it did when this
    test was written against the unclassified config)."""
    outside = tmp_path / "outside.xml"
    config = (
        f'version: "2"\noutput:\n  formats:\n    checkstyle:\n      path: {outside}\n'
    ).encode()

    with caplog.at_level(logging.INFO, logger="adapter.analyzers.golangci_lint"):
        findings = await _run({"a.go": _STATICCHECK}, {".golangci.yml": config})

    assert [f.rule_id for f in findings] == ["SA5000"]
    assert [r.__dict__.get("config_skip_reason") for r in caplog.records] == [
        "output_path_not_allowed"
    ]
    assert not outside.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_repo_text_format_on_stdout_does_not_disturb_the_report() -> None:
    config = b'version: "2"\noutput:\n  formats:\n    text:\n      path: stdout\n'
    findings = await _run({"a.go": _STATICCHECK}, {".golangci.yml": config})
    assert [f.rule_id for f in findings] == ["SA5000"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_run_leaves_no_caches_or_temp_files_outside_its_scratch_directory() -> None:
    def snapshot() -> set[Path]:
        return {
            *Path("/tmp").glob("go-build*"),
            *Path("/tmp").glob("golangci*"),
            *Path("/tmp").glob("*.lock"),
        }

    before = snapshot()
    await _run({"a.go": _STATICCHECK, "b.go": _CLEAN})
    assert snapshot() == before


@pytest.mark.integration
def test_the_pinned_toolchain_is_present_and_new_enough_for_the_generated_go_mod() -> None:
    import subprocess

    from adapter.analyzers.golangci_lint import _GO_MOD

    go_version = subprocess.run(
        ["go", "version"], capture_output=True, text=True, check=True
    ).stdout
    language = _GO_MOD.decode().split("go ")[1].split()[0]
    assert f"go{language}" in go_version
    lint_version = subprocess.run(
        ["golangci-lint", "--version"], capture_output=True, text=True, check=True
    ).stdout
    assert "version 2." in lint_version


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
async def test_go_only_pr_posts_only_golangci_findings_on_added_lines() -> None:
    content = {
        "app.go": (
            b'package app\n\nfunc Old() {\n\tvar m map[string]int\n\tm["old"] = 1\n}\n\n'
            b'func Added() {\n\tvar m map[string]int\n\tm["new"] = 1\n}\n'
        ),
        "main.py": b"import os\n",
    }
    # Only lines 7-11 are added; the pre-existing nil-map write on line 5 is untouched.
    patch = (
        "@@ -1,6 +1,11 @@\n package app\n \n func Old() {\n \tvar m map[string]int\n"
        ' \tm["old"] = 1\n }\n+\n+func Added() {\n+\tvar m map[string]int\n+\tm["new"] = 1\n+}\n'
    )
    diff = Diff(
        files=(
            DiffFile(path="app.go", status="modified", patch=patch, mode="100644"),
            DiffFile(path="main.py", status="added", patch="@@ -0,0 +1 @@\n+import os\n"),
        )
    )
    step = StaticAnalysisStep(
        _FetchesContent(content),
        registrations=[
            AnalyzerRegistration(
                "golangci_lint",
                GolangciLintAnalyzer(),
                matches=match_go,
                changed_lines_only=True,
                config_filenames=GOLANGCI_CONFIG_FILENAMES,
            )
        ],
        redactor=_PassthroughRedactor(),
        enabled={"golangci_lint": True},
        timeouts={"golangci_lint": 60.0},
    )

    from review.step import StepContext

    output = await step.run(_PrefilterOutput(diff=diff), StepContext(job_id="job-1"))

    assert [(f.file, f.line, f.rule_id, f.source) for f in output.findings] == [
        ("app.go", 10, "SA5000", "golangci_lint")
    ]
    json.dumps([f.model_dump() for f in output.findings])  # serializable as posted


def test_alias_bomb_config_is_classified_quickly() -> None:
    lines = ['version: "2"', "a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    lines += [f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 9)}]" for i in range(1, 14)]

    choice = _classify({".golangci.yml": "\n".join(lines) + "\n"})

    assert choice.outcome.skip_reason is not None or choice.config_name == ".golangci.yml"
