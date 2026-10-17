"""Tests for adapter.analyzers.eslint (plan-phase-9 Phase 4): the pure
data-only config classifier and finding normalization at the unit tier, and the
real pinned ``review-eslint`` toolset on fixtures at the integration tier
(US-A2/A3/A4/A5/A6/A9 for JavaScript/TypeScript).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from adapter.analyzers.eslint import (
    _DEFAULT_PROFILE_PATH,
    ESLINT_CONFIG_FILENAMES,
    EslintAnalyzer,
    EslintConfigChoice,
    _parse_output,
    _to_findings,
    classify_eslint_config,
)
from adapter.analyzers.exceptions import AnalyzerOutputParseError
from findings.models import Finding
from ingestion.diff import Diff, DiffFile
from review.file_matchers import match_javascript
from review.static_analysis import AnalyzerRegistration, StaticAnalysisStep

_UNDEFINED_NAME = b"export function f() {\n  return undefinedName;\n}\n"
_UNUSED_VAR = b"export const a = 1;\nconst unused = 2;\n"
# Style-only: spacing, quotes, naming, loose equality - nothing a bug-finder cares about.
_STYLE_ONLY = (
    b"var Bad_Name = 'x'\nfunction  Odd( a ,b ){return a+b}\nif (Bad_Name == 'x') { Odd(1,2) }\n"
    b"export { Bad_Name, Odd }\n"
)
_CLEAN = b"export function add(a, b) {\n  return a + b;\n}\n"


# A repo config with no `extends`: eslintrc defaults to ES5 scripts, so ES modules
# need these parser options. `%s` is the rules body.
_MODULE_CONFIG = (
    b'{"root": true, "parserOptions": {"ecmaVersion": 2022, "sourceType": "module"}, "rules": {%s}}'
)


@pytest.fixture(autouse=True)
def _enable_analyzer_logger() -> None:
    # An earlier test's logging setup may have disabled this already-imported logger.
    logging.getLogger("adapter.analyzers.eslint").disabled = False


def _run(
    content: dict[str, bytes], config_files: dict[str, bytes] | None = None
) -> Coroutine[Any, Any, list[Finding]]:
    return EslintAnalyzer().run(
        Diff(files=()), content, timeout=60, config_files=config_files or {}
    )


def _rules(findings: list[Finding]) -> list[str | None]:
    return sorted((f.rule_id for f in findings), key=lambda r: r or "")


# --- unit: pinned default profile ------------------------------------------------


def test_default_profile_checksum_matches() -> None:
    actual = hashlib.sha256(_DEFAULT_PROFILE_PATH.read_bytes()).hexdigest()
    pinned = _DEFAULT_PROFILE_PATH.with_suffix(".json.sha256").read_text().strip()
    assert actual == pinned, (
        "eslint_default.json changed without eslint_default.json.sha256 being re-pinned"
    )


def test_default_profile_is_a_classifier_clean_data_config() -> None:
    """The shipped default must itself satisfy the data-only rules it enforces."""
    default = _DEFAULT_PROFILE_PATH.read_bytes()
    assert classify_eslint_config({".eslintrc.json": default}).config_name == ".eslintrc.json"


# --- unit: data-only classifier (US-A4) ------------------------------------------


def _classify(files: dict[str, str]) -> EslintConfigChoice:
    return classify_eslint_config({name: text.encode() for name, text in files.items()})


def test_no_config_uses_the_default_profile_silently() -> None:
    choice = _classify({})
    assert choice.config_name is None
    assert choice.outcome.source == "default"
    assert choice.outcome.skip_reason is None


@pytest.mark.parametrize("name", [".eslintrc.json", ".eslintrc.yaml", ".eslintrc.yml"])
def test_data_config_forms_are_honored(name: str) -> None:
    text = "root: true\nrules: {eqeqeq: error}\n"  # YAML, and also JSON-compatible flow style
    choice = _classify({name: text if name != ".eslintrc.json" else '{"root": true}'})
    assert choice.config_name == name
    assert choice.outcome.source == "repo"
    assert name in choice.write_names


def test_package_json_eslint_config_is_honored_and_without_it_is_ignored() -> None:
    honored = _classify({"package.json": '{"eslintConfig": {"root": true}}'})
    assert honored.config_name == "package.json"
    ignored = _classify({"package.json": '{"name": "x"}'})
    assert ignored.config_name is None
    assert ignored.outcome.skip_reason is None


def test_legacy_precedence_is_yaml_then_yml_then_json_then_package_json() -> None:
    files = {
        "package.json": '{"eslintConfig": {}}',
        ".eslintrc.json": "{}",
        ".eslintrc.yml": "{}",
        ".eslintrc.yaml": "{}",
    }
    assert _classify(files).config_name == ".eslintrc.yaml"
    del files[".eslintrc.yaml"]
    assert _classify(files).config_name == ".eslintrc.yml"
    del files[".eslintrc.yml"]
    assert _classify(files).config_name == ".eslintrc.json"
    del files[".eslintrc.json"]
    assert _classify(files).config_name == "package.json"


@pytest.mark.parametrize(
    "name", [n for n in ESLINT_CONFIG_FILENAMES if n.startswith("eslint.config")]
)
def test_flat_config_is_executable_and_wins_over_a_data_config(name: str) -> None:
    choice = _classify({name: "module.exports = []", ".eslintrc.json": "{}"})
    assert choice.config_name is None
    assert choice.outcome.source == "default"
    assert choice.outcome.skip_reason == "executable_config"
    assert choice.skipped_path == name
    assert choice.write_names == ()


@pytest.mark.parametrize("name", [".eslintrc.js", ".eslintrc.cjs"])
def test_executable_legacy_config_is_skipped_even_beside_a_data_config(name: str) -> None:
    choice = _classify({name: "module.exports = {}", ".eslintrc.json": "{}"})
    assert (choice.config_name, choice.outcome.skip_reason) == (None, "executable_config")
    assert choice.skipped_path == name


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ({".eslintrc.json": '{"extends": "airbnb"}'}, "extends_unresolved"),
        ({".eslintrc.json": '{"extends": ["prettier"]}'}, "extends_unresolved"),
        ({".eslintrc.json": '{"extends": "plugin:react/recommended"}'}, "extends_unresolved"),
        ({".eslintrc.json": '{"extends": "/etc/eslintrc.json"}'}, "extends_unresolved"),
        ({".eslintrc.json": '{"extends": "./evil.js"}'}, "extends_unresolved"),
        ({".eslintrc.json": '{"extends": "./missing.json"}'}, "extends_unresolved"),
        ({".eslintrc.json": '{"extends": "../.eslintrc.json"}'}, "extends_unresolved"),
        ({".eslintrc.json": '{"extends": "./.eslintrc.json"}'}, "extends_unresolved"),
        (
            {
                ".eslintrc.json": '{"extends": "./.eslintrc.yaml"}',
                ".eslintrc.yaml": "extends: ./.eslintrc.json\n",
            },
            "extends_unresolved",
        ),
        (
            {".eslintrc.json": '{"extends": "plugin:@typescript-eslint/recommended-type-checked"}'},
            "type_aware_not_supported",
        ),
        ({".eslintrc.json": '{"plugins": ["react"]}'}, "plugin_unresolved"),
        ({".eslintrc.json": '{"plugins": ["../evil"]}'}, "plugin_unresolved"),
        ({".eslintrc.json": '{"plugins": ["@typescript-eslint/../../evil"]}'}, "plugin_unresolved"),
        ({".eslintrc.json": '{"rules": {"import/no-cycle": "error"}}'}, "plugin_unresolved"),
        ({".eslintrc.json": '{"processor": "markdown/markdown"}'}, "plugin_unresolved"),
        ({".eslintrc.json": '{"parser": "@babel/eslint-parser"}'}, "parser_unresolved"),
        ({".eslintrc.json": '{"parser": "./evil.js"}'}, "parser_unresolved"),
        (
            {".eslintrc.json": '{"parserOptions": {"project": "./tsconfig.json"}}'},
            "type_aware_not_supported",
        ),
        (
            {".eslintrc.json": '{"parserOptions": {"tsconfigRootDir": "/etc"}}'},
            "type_aware_not_supported",
        ),
        (
            {".eslintrc.json": '{"parserOptions": {"projectService": true}}'},
            "type_aware_not_supported",
        ),
        (
            {".eslintrc.json": '{"overrides": [{"files": ["*.ts"], "parser": "nope"}]}'},
            "parser_unresolved",
        ),
        (
            {".eslintrc.json": '{"overrides": [{"files": ["*.ts"], "plugins": ["x"]}]}'},
            "plugin_unresolved",
        ),
        ({".eslintrc.json": '{"overrides": 3}'}, "config_invalid"),
        ({".eslintrc.json": '{"plugins": 3}'}, "config_invalid"),
        ({".eslintrc.json": "{bad"}, "config_invalid"),
        ({".eslintrc.json": '// comment\n{"root": true}'}, "config_invalid"),
        ({".eslintrc.json": "[1, 2]"}, "config_invalid"),
        ({".eslintrc.yaml": "key: [unclosed"}, "config_invalid"),
        ({"package.json": "{bad"}, "config_invalid"),
    ],
)
def test_unsafe_or_unresolved_config_falls_back_to_default_with_a_reason(
    files: dict[str, str], reason: str
) -> None:
    choice = _classify(files)
    assert choice.config_name is None
    assert choice.outcome.source == "default"
    assert choice.outcome.skip_reason == reason
    assert choice.skipped_path in ESLINT_CONFIG_FILENAMES


def test_pinned_plugin_parser_and_relative_extends_are_honored() -> None:
    choice = _classify(
        {
            ".eslintrc.yaml": json.dumps(
                {
                    "extends": ["eslint:recommended", "./.eslintrc.json"],
                    "parser": "@typescript-eslint/parser",
                    "plugins": ["@typescript-eslint"],
                    "rules": {"@typescript-eslint/no-explicit-any": "warn", "eqeqeq": "error"},
                    "overrides": [
                        {
                            "files": ["*.ts"],
                            "extends": "plugin:@typescript-eslint/recommended",
                            "parser": "@typescript-eslint/parser",
                        }
                    ],
                }
            ),
            ".eslintrc.json": '{"rules": {"no-console": "warn"}}',
        }
    )
    assert choice.config_name == ".eslintrc.yaml"
    assert choice.write_names == (".eslintrc.json", ".eslintrc.yaml")


def test_extends_target_is_judged_too() -> None:
    choice = _classify(
        {
            ".eslintrc.yaml": '{"extends": "./.eslintrc.json"}',
            ".eslintrc.json": '{"plugins": ["x"]}',
        }
    )
    assert choice.outcome.skip_reason == "plugin_unresolved"


def test_aliased_yaml_cannot_fan_the_walk_out() -> None:
    """A billion-laughs-shaped config (every override aliases the previous
    level's list) must classify in bounded time."""
    levels = ["l0: &l0 [{files: ['*'], rules: {}}]"]
    for i in range(1, 30):
        entry = f"{{files: ['*'], overrides: *l{i - 1}}}"
        levels.append(f"l{i}: &l{i} [{entry}, {entry}]")
    text = "\n".join(levels) + "\noverrides: *l29\n"

    started = time.monotonic()
    choice = _classify({".eslintrc.yaml": text})

    assert time.monotonic() - started < 2  # 2**30 paths if each were walked
    assert choice.config_name == ".eslintrc.yaml"


def test_many_overrides_extending_one_file_walks_it_once() -> None:
    overrides = [{"files": [f"{i}.js"], "extends": "./.eslintrc.json"} for i in range(2000)]
    started = time.monotonic()
    choice = _classify(
        {".eslintrc.yaml": json.dumps({"overrides": overrides}), ".eslintrc.json": "{}"}
    )
    assert choice.config_name == ".eslintrc.yaml"
    assert time.monotonic() - started < 2


# --- unit: output parsing and normalization (US-A2) ------------------------------


def _entry(*messages: dict[str, Any], path: str = "/scan/a.js") -> dict[str, Any]:
    return {"filePath": path, "messages": list(messages)}


def _msg(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "ruleId": "no-undef",
        "severity": 2,
        "message": "'x' is not defined.",
        "line": 7,
        "fatal": False,
    }
    return {**message, **overrides}


def test_finding_normalization_maps_rule_and_eslint_severity() -> None:
    [f] = _to_findings(_entry(_msg()), Path("/scan"), {"a.js": "orig/a.js"})
    assert (f.file, f.line, f.severity, f.source, f.rule_id) == (
        "orig/a.js",
        7,
        "high",
        "eslint",
        "no-undef",
    )

    def one(**overrides: Any) -> Finding:
        return _to_findings(_entry(_msg(**overrides)), Path("/scan"), {})[0]

    assert one(ruleId="no-unused-vars").severity == "low"
    assert one(ruleId="@typescript-eslint/no-unused-vars").severity == "low"
    # Not a pinned rule: ESLint's own level decides; unmapped defaults to medium.
    assert one(ruleId="eqeqeq", severity=2).severity == "medium"
    assert one(ruleId="eqeqeq", severity=1).severity == "low"
    assert one(ruleId="eqeqeq", severity=9).severity == "medium"
    assert one(ruleId="eqeqeq", severity=None).severity == "medium"
    assert one(ruleId="eqeqeq", severity="2").severity == "medium"
    # A parse error has no rule and is fatal.
    parse_error = one(ruleId=None, fatal=True, message="Parsing error: x")
    assert (parse_error.rule_id, parse_error.severity) == (None, "high")
    assert one(line=None).line is None
    assert one(line=0).line is None


@pytest.mark.parametrize(
    "text",
    [
        'File ignored because of a matching ignore pattern. Use "--no-ignore" to disable.',
        "File ignored by default. Use a negated ignore pattern to override.",
        "Definition for rule 'react/jsx-key' was not found.",
    ],
)
def test_noise_that_is_not_a_finding_about_the_code_is_dropped(text: str) -> None:
    assert (
        _to_findings(_entry(_msg(ruleId=None, severity=1, message=text)), Path("/scan"), {}) == []
    )


def test_dotdot_prefixed_filename_is_inside_the_scan_directory() -> None:
    [f] = _to_findings(_entry(_msg(), path="/scan/..x.js"), Path("/scan"), {})
    assert f.file == "..x.js"


@pytest.mark.parametrize(
    "entry",
    [
        {"filePath": "/scan/a.js"},
        {"messages": []},
        _entry(_msg(), path="/elsewhere/a.js"),
        _entry(_msg(message=3)),
        _entry({"ruleId": "x"}),
        {"filePath": None, "messages": []},
        {"filePath": "/scan/a.js", "messages": "no"},
        _entry("not a dict"),  # type: ignore[arg-type]
    ],
)
def test_malformed_entries_raise_parse_error(entry: dict[str, Any]) -> None:
    with pytest.raises(AnalyzerOutputParseError):
        _to_findings(entry, Path("/scan"), {})


@pytest.mark.parametrize("raw", [b"not json", b"\xff", b'{"a": 1}'])
def test_garbage_output_raises_parse_error(raw: bytes) -> None:
    with pytest.raises(AnalyzerOutputParseError):
        _parse_output(raw)


@pytest.mark.asyncio
async def test_empty_content_short_circuits_without_spawning_eslint() -> None:
    assert await _run({}) == []


# --- integration: real pinned toolset -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_correctness_violations_are_reported_with_original_paths() -> None:
    findings = await _run(
        {
            "pkg/a.js": _UNDEFINED_NAME,
            "b.js": _UNUSED_VAR,
            "c.ts": b"export const n: number = 1;\nconst unusedTs: string = 'a';\n",
            "broken.js": b"var z = ;\n",
        }
    )

    by_rule = {(f.file, f.rule_id) for f in findings}
    assert ("pkg/a.js", "no-undef") in by_rule
    assert ("b.js", "no-unused-vars") in by_rule
    assert ("c.ts", "@typescript-eslint/no-unused-vars") in by_rule
    assert ("broken.js", None) in by_rule
    assert all(f.source == "eslint" for f in findings)
    undefined = next(f for f in findings if f.rule_id == "no-undef")
    assert (undefined.line, undefined.severity) == (2, "high")
    broken = next(f for f in findings if f.file == "broken.js")
    assert (broken.severity, broken.message.startswith("Parsing error")) == ("high", True)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_default_profile_ignores_style_only_violations_and_clean_code() -> None:
    assert await _run({"style.js": _STYLE_ONLY, "clean.js": _CLEAN}) == []
    seeded = await _run({"style.js": _STYLE_ONLY + b"\nconsole.log(undefinedName);\n"})
    assert [f.rule_id for f in seeded] == ["no-undef"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_typescript_and_jsx_parse_under_the_default_profile() -> None:
    content = {
        "ok.ts": b"interface P { a: number }\nexport const f = (p: P): number => p.a;\n",
        "ok.tsx": b"export const C = (): JSX.Element => <div>{1}</div>;\n",
        "ok.jsx": b"export const D = () => <span>hi</span>;\n",
        "ok.mjs": b"import { f } from './ok.ts';\nexport default f;\n",
        "ok.cjs": b"module.exports = { a: 1 };\n",
    }
    assert await _run(content) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_findings_found_is_success_not_an_invocation_error() -> None:
    # Would raise AnalyzerInvocationError if ESLint's exit 1 were not mapped to success.
    assert await _run({"a.js": _UNDEFINED_NAME})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dash_prefixed_filename_is_a_path_not_a_flag() -> None:
    findings = await _run({"-x.js": _UNDEFINED_NAME})
    assert [(f.file, f.rule_id) for f in findings] == [("-x.js", "no-undef")]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_large_source_does_not_inflate_output_past_the_cap() -> None:
    """The compact formatter omits the source echo the built-in json one adds."""
    # 12 MB of source (past the 10 MB cap) with one finding: only an echo of the
    # source in the output could trip the cap.
    big = b"// " + b"a" * 78 + b"\n"
    content = big * 150_000 + b"export const f = () => undefinedName;\n"
    findings = await _run({"big.js": content})
    assert [f.rule_id for f in findings] == ["no-undef"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repo_config_governs_which_rules_apply() -> None:
    content = {"a.js": b"export const f = (x) => x == 1;\nconst unused = 1;\n"}
    config = {".eslintrc.json": _MODULE_CONFIG % b'"eqeqeq": "error"'}

    findings = await _run(content, config)

    assert _rules(findings) == ["eqeqeq"]  # honored: no no-unused-vars from the default profile
    assert _rules(await _run(content)) == ["no-unused-vars"]  # default profile


@pytest.mark.asyncio
@pytest.mark.integration
async def test_yaml_and_package_json_configs_are_honored() -> None:
    content = {"a.js": b"console.log(1 == 1);\n"}
    yaml_config = {".eslintrc.yaml": b"root: true\nrules:\n  no-console: error\n"}
    package_config = {
        "package.json": b'{"name": "x", "eslintConfig": {"root": true, "rules": {"eqeqeq": 2}}}'
    }

    assert _rules(await _run(content, yaml_config)) == ["no-console"]
    assert _rules(await _run(content, package_config)) == ["eqeqeq"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_extends_a_fetched_data_config_and_a_pinned_plugin_preset_resolve() -> None:
    config = {
        ".eslintrc.yaml": json.dumps(
            {
                "root": True,
                "parserOptions": {"ecmaVersion": 2022, "sourceType": "module"},
                "extends": ["./.eslintrc.json"],
                "overrides": [
                    {
                        "files": ["*.ts"],
                        "parser": "@typescript-eslint/parser",
                        "extends": ["plugin:@typescript-eslint/recommended"],
                    }
                ],
            }
        ).encode(),
        ".eslintrc.json": b'{"rules": {"eqeqeq": "error"}}',
    }
    content = {"a.js": b"export const f = (x) => x == 1;\n", "b.ts": b"export const v: any = 1;\n"}

    assert sorted(f.rule_id or "" for f in await _run(content, config)) == [
        "@typescript-eslint/no-explicit-any",
        "eqeqeq",
    ]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_inline_directives_are_honored_and_unknown_plugin_rules_are_quiet() -> None:
    content = {
        "a.js": (
            b"/* eslint-disable no-undef */\nexport const f = () => nope;\n"
            b"/* eslint-enable no-undef */\n"
            b"// eslint-disable-next-line react/jsx-key\nexport const g = 1;\n"
            b"/* eslint react/jsx-key: 2 */\nexport const h = () => alsoNope;\n"
        )
    }
    findings = await _run(content)
    assert [(f.rule_id, f.line) for f in findings] == [("no-undef", 7)]


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("config", "reason"),
    [
        ({".eslintrc.json": b'{"extends": ["prettier"]}'}, "extends_unresolved"),
        ({".eslintrc.json": b'{"plugins": ["react"]}'}, "plugin_unresolved"),
        ({".eslintrc.json": b'{"parser": "@babel/eslint-parser"}'}, "parser_unresolved"),
        ({".eslintrc.json": b'{"extends": "./gone.json"}'}, "extends_unresolved"),
    ],
)
async def test_unresolved_reference_falls_back_to_default_and_logs_it(
    config: dict[str, bytes], reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="adapter.analyzers.eslint"):
        findings = await _run({"a.js": _UNDEFINED_NAME}, config)

    assert _rules(findings) == ["no-undef"]  # default profile ran; the job did not fail
    selected = [r for r in caplog.records if r.__dict__.get("config_skip_reason")]
    assert [r.__dict__["config_skip_reason"] for r in selected] == [reason]
    assert selected[0].__dict__["config_path"] == ".eslintrc.json"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_config_the_tool_rejects_is_rerun_once_with_the_default_profile(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Valid data the classifier accepts, but ESLint rejects (bad rule options).
    config = {".eslintrc.json": b'{"rules": {"eqeqeq": ["error", "not-an-option"]}}'}

    with caplog.at_level(logging.WARNING, logger="adapter.analyzers.eslint"):
        findings = await _run({"a.js": _UNDEFINED_NAME}, config)

    assert _rules(findings) == ["no-undef"]
    assert [r.__dict__.get("config_skip_reason") for r in caplog.records] == ["rejected_by_tool"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ignore_patterns_in_the_repo_config_exclude_files_quietly() -> None:
    config = {
        ".eslintrc.json": (
            b'{"root": true, "ignorePatterns": ["gen/**"], "rules": {"no-undef": 2}, '
            b'"parserOptions": {"ecmaVersion": 2022, "sourceType": "module"}}'
        )
    }
    findings = await _run({"gen/a.js": _UNDEFINED_NAME, "b.js": _UNDEFINED_NAME}, config)
    assert [(f.file, f.rule_id) for f in findings] == [("b.js", "no-undef")]


# Each executable config form, written so that merely *loading* it would create
# `marker`. ESLint is a Node program that `require`s these, so this is the
# highest-severity case of plan-phase-9 Phase 4 (the worker holds credentials).
def _marker_js(marker: Path) -> bytes:
    return f"require('fs').writeFileSync({str(marker)!r}, 'ran'); module.exports = {{}};\n".encode()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "name",
    [
        "eslint.config.js",
        "eslint.config.mjs",
        "eslint.config.cjs",
        "eslint.config.ts",
        ".eslintrc.js",
        ".eslintrc.cjs",
    ],
)
async def test_executable_config_forms_are_never_run(
    name: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    marker = tmp_path / "marker"
    config = {name: _marker_js(marker)}

    with caplog.at_level(logging.INFO, logger="adapter.analyzers.eslint"):
        findings = await _run({"a.js": _UNDEFINED_NAME}, config)

    assert not marker.exists()
    assert _rules(findings) == ["no-undef"]  # default profile ran instead
    skips = [r.__dict__.get("config_skip_reason") for r in caplog.records]
    assert skips == ["executable_config"]


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "config_json",
    [
        '{"extends": "./evil.js"}',
        '{"parser": "./evil.js"}',
        '{"plugins": ["../evil"]}',
        '{"plugins": ["@typescript-eslint/../../evil"]}',
        '{"overrides": [{"files": ["*.js"], "extends": "./evil.js"}]}',
        '{"processor": "./evil.js"}',
    ],
)
async def test_config_references_to_repo_code_never_load_it(
    config_json: str, tmp_path: Path
) -> None:
    """Canary: ``evil.js`` is part of the scanned content, so a config that
    could reach it (by extends, parser, plugin or processor) would run it."""
    marker = tmp_path / "marker"
    content = {
        "a.js": _UNDEFINED_NAME,
        "evil.js": _marker_js(marker),
        "evil/index.js": _marker_js(marker),
    }

    findings = await _run(content, {".eslintrc.json": config_json.encode()})

    assert not marker.exists()
    assert "no-undef" in {f.rule_id for f in findings}


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("config_path", "config_json", "shadowed"),
    [
        (
            ".eslintrc.json",
            '{"root": true, "parser": "espree", "extends": "eslint:recommended",'
            ' "parserOptions": {"sourceType": "module", "ecmaVersion": 2022}}',
            "node_modules/espree/index.js",
        ),
        (
            ".eslintrc.json",
            '{"root": true, "extends": "eslint:recommended",'
            ' "parser": "@typescript-eslint/parser"}',
            "node_modules/@typescript-eslint/parser/index.js",
        ),
        (None, None, "node_modules/@typescript-eslint/parser/index.js"),
        (None, None, "sub\\node_modules/espree/index.js"),
    ],
)
async def test_node_modules_cannot_shadow_the_pinned_parser(
    config_path: str | None, config_json: str | None, shadowed: str, tmp_path: Path
) -> None:
    """Canary: a PR-supplied node_modules/<allow-listed parser> must never load."""
    marker = tmp_path / "marker"
    content = {"a.js": _UNDEFINED_NAME}
    if config_path is None:
        content["b.ts"] = b"export const n: number = 1;\n"  # loads the default TS override
    content[shadowed] = _marker_js(marker)
    config = {config_path: config_json.encode()} if config_path and config_json else {}

    findings = await _run(content, config)

    assert not marker.exists()
    assert "no-undef" in {f.rule_id for f in findings}


@pytest.mark.asyncio
async def test_only_node_modules_content_lints_nothing() -> None:
    assert await _run({"node_modules/x/index.js": _UNDEFINED_NAME}) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_scanned_repo_code_is_linted_but_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    code = _marker_js(marker)
    content = {"a.js": code, "setup.js": code, "jest.config.js": code, "webpack.config.cjs": code}
    config = {"package.json": b'{"main": "a.js", "scripts": {"postinstall": "node a.js"}}'}

    await _run(content, config)

    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_paths_outside_the_scan_directory_are_skipped_and_nothing_is_written(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Canary: a config pointing the TypeScript parser at an outside directory
    is skipped, and neither that path nor ESLint's cache location is created."""
    outside = tmp_path / "outside"
    config = {
        ".eslintrc.json": json.dumps(
            {"parserOptions": {"tsconfigRootDir": str(outside), "project": "tsconfig.json"}}
        ).encode()
    }

    with caplog.at_level(logging.INFO, logger="adapter.analyzers.eslint"):
        findings = await _run({"a.ts": b"export const a: number = 1;\nconst u = 1;\n"}, config)

    assert [f.rule_id for f in findings] == ["@typescript-eslint/no-unused-vars"]
    assert [r.__dict__.get("config_skip_reason") for r in caplog.records] == [
        "type_aware_not_supported"
    ]
    assert not outside.exists()
    assert not list(tmp_path.glob("**/.eslintcache"))


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
async def test_js_only_pr_posts_only_eslint_findings_on_added_lines() -> None:
    content = {
        "app.js": b"const unusedOld = 1;\n\nexport function f() {\n  return undefinedName;\n}\n",
        "main.go": b"package main\n",
    }
    # Only line 4 (the undefined name) is added; the unused var on line 1 is untouched.
    patch = (
        "@@ -1,4 +1,5 @@\n const unusedOld = 1;\n \n export function f() {\n"
        "+  return undefinedName;\n }\n"
    )
    diff = Diff(
        files=(
            DiffFile(path="app.js", status="modified", patch=patch, mode="100644"),
            DiffFile(path="main.go", status="added", patch="@@ -0,0 +1 @@\n+package main\n"),
        )
    )
    step = StaticAnalysisStep(
        _FetchesContent(content),
        registrations=[
            AnalyzerRegistration(
                "eslint",
                EslintAnalyzer(),
                matches=match_javascript,
                changed_lines_only=True,
                config_filenames=ESLINT_CONFIG_FILENAMES,
            )
        ],
        redactor=_PassthroughRedactor(),
        enabled={"eslint": True},
        timeouts={"eslint": 30.0},
    )

    from review.step import StepContext

    output = await step.run(_PrefilterOutput(diff=diff), StepContext(job_id="job-1"))

    assert [(f.file, f.line, f.rule_id, f.source) for f in output.findings] == [
        ("app.js", 4, "no-undef", "eslint")
    ]
    json.dumps([f.model_dump() for f in output.findings])  # serializable as posted


@pytest.mark.parametrize(
    "raw",
    [b'{"parser": ["x"]}', b'{"parser": {"a": 1}}', b"[" * 100_000 + b"]" * 100_000],
)
def test_hostile_config_falls_back_to_default_not_a_crash(raw: bytes) -> None:
    for name in (".eslintrc.json", ".eslintrc.yaml"):
        choice = classify_eslint_config({name: raw})
        assert choice.config_name is None
        assert choice.outcome.source == "default"
