"""adapter.analyzers.eslint — the concrete ``StaticAnalyzer`` backed by the
pinned ESLint toolset (``review-eslint`` on ``PATH``: Node + ESLint 9 in legacy
eslintrc mode + a fixed plugin set, installed from ``tools/eslint`` at image
build, per docs/plans/plan-phase-9.md Phase 4).

**Config is data, never code.** ``classify_eslint_config`` (pure, unit-tested)
decides which fetched root config ESLint may be pointed at. Executable forms
(``eslint.config.*``, ``.eslintrc.js``/``.cjs``) are never written to disk or
loaded; a data config (``.eslintrc.json``/``.yaml``/``.yml``, or
``package.json`` ``eslintConfig``) is honored only when every ``extends``,
``plugins``, ``parser`` and plugin-prefixed rule resolves inside the pinned
set or a fetched data config, and it asks for no type-aware linting. Anything
else falls back to the pinned default profile (``eslint_default.json``) with a
logged ``config_skip_reason``. A config ESLint itself rejects (exit 2) is
re-run once with the default profile.

**Isolation:** ``--no-eslintrc -c <file>`` reads only the selected config;
the wrapper resolves plugins/parsers only from the pinned toolset, never
passes ``--cache``/``--output-file``/``--fix``, and the ``--`` end-of-options
guard stops a PR-controlled ``-x.js`` filename being parsed as a flag.
Findings-found is success (the wrapper maps ESLint's exit 1 to 0).

shortcut: ``.eslintrc.json`` with ``//`` comments (legal for ESLint) is not
valid JSON here and falls back to the default profile as ``config_invalid``;
add a comment stripper if pilot repos hit it. Only the repo-root config is
read (plan Assumption 9), and ``.eslintignore`` is not fetched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
import re
import shutil
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from adapter.analyzers.exceptions import AnalyzerInvocationError, AnalyzerOutputParseError
from adapter.analyzers.subprocess_runner import (
    cleanup_temp_directory,
    materialize_into_temp_directory,
    original_path_mapping,
    run_analyzer_subprocess,
)
from findings.models import Finding, Severity
from ingestion.diff import Diff
from review.static_analysis import ConfigOutcome, StaticAnalyzer

logger = logging.getLogger(__name__)

_DEFAULT_PROFILE_PATH = Path(__file__).parent / "eslint_default.json"

# ESLint 9 (flat config on by default) uses these exclusively when present, so
# a repo carrying one is configured by code we will not run.
_FLAT_CONFIG_FILENAMES = tuple(
    f"eslint.config.{ext}" for ext in ("js", "mjs", "cjs", "ts", "mts", "cts")
)
_EXECUTABLE_LEGACY_FILENAMES = (".eslintrc.js", ".eslintrc.cjs")
# Legacy lookup order within one directory (after the executable forms).
_DATA_CONFIG_FILENAMES = (".eslintrc.yaml", ".eslintrc.yml", ".eslintrc.json", "package.json")
# The executable names are fetched only so their presence is seen and the skip
# logged; they are never written to the scan directory.
ESLINT_CONFIG_FILENAMES = (
    *_FLAT_CONFIG_FILENAMES,
    *_EXECUTABLE_LEGACY_FILENAMES,
    *_DATA_CONFIG_FILENAMES,
)
_EXTENDABLE_FILENAMES = frozenset({".eslintrc.yaml", ".eslintrc.yml", ".eslintrc.json"})

# The plugin references tools/eslint/package.json installs (short and long form).
_PINNED_PLUGIN_REFS = frozenset({"@typescript-eslint", "@typescript-eslint/eslint-plugin"})
_PINNED_PARSERS = frozenset({"espree", "@typescript-eslint/parser"})
_BUILTIN_EXTENDS = frozenset({"eslint:recommended", "eslint:all"})
# Type-aware linting reads tsconfig files (possibly outside the scan directory)
# and needs resolved dependencies the scan never has.
_TYPE_AWARE_OPTIONS = frozenset(
    {"project", "projectService", "EXPERIMENTAL_useProjectService", "tsconfigRootDir"}
)

_ESLINT_CONFIG_REJECTED_EXIT_CODE = 2

_RULE_SEVERITY: dict[str, Severity] = {
    "no-undef": "high",
    "no-unused-vars": "low",
    "@typescript-eslint/no-unused-vars": "low",
}
_LEVEL_SEVERITY: dict[int, Severity] = {2: "medium", 1: "low"}
_DEFAULT_SEVERITY: Severity = "medium"
_FATAL_SEVERITY: Severity = "high"  # a parse error: the changed file does not even parse

# Noise that is not a finding about the changed code: an explicitly named file
# the repo's own ignore patterns exclude, and a rule from a plugin outside the
# pinned set (named by an inline comment, since configs naming one are skipped).
_NOT_A_FINDING = re.compile(r"^(File ignored |Definition for rule '.*' was not found)")


@dataclass(frozen=True)
class EslintConfigChoice:
    """``config_name`` is the fetched root file to pass to ``-c``, or ``None``
    to use the pinned default profile. ``write_names`` are the data files the
    chosen config needs on disk (itself plus any ``extends`` targets)."""

    config_name: str | None
    outcome: ConfigOutcome
    skipped_path: str | None = None
    write_names: tuple[str, ...] = ()


def _load_config(name: str, raw: bytes) -> dict[str, Any] | None:
    """The ESLint config object in ``name``; ``None`` when ``package.json`` has
    no ``eslintConfig``. Raises ``ValueError`` for anything unparseable."""
    try:
        text = raw.decode("utf-8")
        document = yaml.safe_load(text) if name.endswith((".yaml", ".yml")) else json.loads(text)
    except (UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(name) from exc
    if name == "package.json":
        document = document.get("eslintConfig") if isinstance(document, dict) else None
        if document is None:
            return None
    if not isinstance(document, dict):
        raise ValueError(name)
    return document


def _string_list(value: object) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


class _ConfigWalk:
    """One classification run. ``memo`` makes each fetched file be walked once
    however many ``extends`` entries name it (so a hostile config cannot fan
    the walk out), and ``stack`` rejects ``extends`` cycles."""

    def __init__(self, files: Mapping[str, bytes]) -> None:
        self._files = files
        self._memo: dict[str, str | None] = {}
        self._stack: list[str] = []

    def reason(self, name: str) -> str | None:
        """Why ``name`` (and everything it ``extends``) cannot be honored, or
        ``None`` when it is plain, self-contained data."""
        if name in self._memo:
            return self._memo[name]
        self._stack.append(name)
        try:
            result = self._file_reason(name)
        finally:
            self._stack.pop()
        self._memo[name] = result
        return result

    def _file_reason(self, name: str) -> str | None:
        try:
            config = _load_config(name, self._files[name])
        except ValueError:
            return "config_invalid"
        if config is None:
            return "config_invalid"
        # Aliased YAML nodes share one object, so a visited-set keeps this linear.
        visited: set[int] = set()
        pending = [config]
        while pending:
            scope = pending.pop()
            if id(scope) in visited:
                continue
            visited.add(id(scope))
            reason = self._scope_reason(scope)
            if reason is not None:
                return reason
            overrides = scope.get("overrides", [])
            if not isinstance(overrides, list) or not all(isinstance(o, dict) for o in overrides):
                return "config_invalid"
            pending.extend(overrides)
        return None

    def _scope_reason(self, scope: dict[str, Any]) -> str | None:
        """One config object, or one ``overrides`` entry."""
        extends = _string_list(scope.get("extends", []))
        plugins = _string_list(scope.get("plugins", []))
        if extends is None or plugins is None:
            return "config_invalid"
        if any(ref not in _PINNED_PLUGIN_REFS for ref in plugins):
            return "plugin_unresolved"
        for ref in extends:
            reason = self._extends_reason(ref)
            if reason is not None:
                return reason
        parser = scope.get("parser")
        if parser is not None and parser not in _PINNED_PARSERS:
            return "parser_unresolved"
        processor = scope.get("processor")
        if isinstance(processor, str) and processor.rpartition("/")[0] not in _PINNED_PLUGIN_REFS:
            return "plugin_unresolved"
        options = scope.get("parserOptions")
        if isinstance(options, dict) and _TYPE_AWARE_OPTIONS & options.keys():
            return "type_aware_not_supported"
        rules = scope.get("rules")
        if isinstance(rules, dict):
            for rule in rules:
                prefix = rule.rpartition("/")[0] if isinstance(rule, str) else ""
                if prefix and prefix not in _PINNED_PLUGIN_REFS:
                    return "plugin_unresolved"
        return None

    def _extends_reason(self, ref: str) -> str | None:
        if ref in _BUILTIN_EXTENDS:
            return None
        if ref.startswith("plugin:"):
            package, _, preset = ref.removeprefix("plugin:").rpartition("/")
            if package not in _PINNED_PLUGIN_REFS:
                return "extends_unresolved"
            return "type_aware_not_supported" if "type-checked" in preset else None
        if ref.startswith(("./", "../")):
            resolved = posixpath.normpath(ref)
            if (
                resolved not in _EXTENDABLE_FILENAMES
                or resolved not in self._files
                or resolved in self._stack
            ):
                return "extends_unresolved"
            return self.reason(resolved)
        # A bare shareable-config name (airbnb, prettier, ...) is outside the pinned set.
        return "extends_unresolved"


def _segments(path: str) -> list[str]:
    return path.replace("\\", "/").split("/")


def classify_eslint_config(config_files: Mapping[str, bytes]) -> EslintConfigChoice:
    """``_classify_eslint_config`` that treats a pathological config (unhashable
    ``parser``, nesting deep enough to overflow the stack) as invalid, never as
    a crash."""
    try:
        return _classify_eslint_config(config_files)
    except (RecursionError, TypeError):
        return EslintConfigChoice(None, ConfigOutcome("default", "config_invalid"))


def _classify_eslint_config(config_files: Mapping[str, bytes]) -> EslintConfigChoice:
    """Pure data-only classifier (no I/O): which fetched config, if any,
    ESLint may be pointed at. Mirrors ESLint 9's own lookup (flat config wins,
    then the legacy order) so the config skipped is the one ESLint would have
    used."""
    for name in (*_FLAT_CONFIG_FILENAMES, *_EXECUTABLE_LEGACY_FILENAMES):
        if name in config_files:
            return EslintConfigChoice(None, ConfigOutcome("default", "executable_config"), name)
    for name in _DATA_CONFIG_FILENAMES:
        if name not in config_files:
            continue
        try:
            is_eslint_config = _load_config(name, config_files[name]) is not None
        except ValueError:
            return EslintConfigChoice(None, ConfigOutcome("default", "config_invalid"), name)
        if not is_eslint_config:
            continue  # a package.json with no eslintConfig is not an ESLint config
        reason = _ConfigWalk(config_files).reason(name)
        if reason is not None:
            return EslintConfigChoice(None, ConfigOutcome("default", reason), name)
        write = sorted({name, *(_EXTENDABLE_FILENAMES & config_files.keys())})
        return EslintConfigChoice(name, ConfigOutcome("repo"), write_names=tuple(write))
    return EslintConfigChoice(None, ConfigOutcome("default"))


def _argv(config_name: str, paths: list[str]) -> list[str]:
    return [
        "review-eslint",
        # Read only the selected config: no cascade, no home-directory config.
        "--no-eslintrc",
        "-c",
        config_name,
        "--",
        *paths,
    ]


def _parse_output(raw: bytes) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AnalyzerOutputParseError(tool="eslint", detail=str(exc)) from exc
    if not isinstance(parsed, list):
        raise AnalyzerOutputParseError(tool="eslint", detail="output was not a JSON list")
    return parsed


def _severity(rule: str | None, level: object, fatal: bool) -> Severity:
    if fatal:
        return _FATAL_SEVERITY
    by_rule = _RULE_SEVERITY.get(rule) if rule else None
    if by_rule is not None:
        return by_rule
    return (
        _LEVEL_SEVERITY.get(level, _DEFAULT_SEVERITY)
        if isinstance(level, int)
        else (_DEFAULT_SEVERITY)
    )


def _to_findings(
    entry: dict[str, Any], directory: Path, original_by_normalized: dict[str, str]
) -> list[Finding]:
    try:
        filename = entry["filePath"]
        messages = entry["messages"]
    except (KeyError, TypeError) as exc:
        raise AnalyzerOutputParseError(tool="eslint", detail=str(exc)) from exc
    if not isinstance(filename, str) or not isinstance(messages, list):
        raise AnalyzerOutputParseError(tool="eslint", detail="malformed entry")

    relative = os.path.relpath(os.path.realpath(filename), os.path.realpath(directory))
    # Segment compare: a file literally named `..x.js` is inside the directory.
    if relative == ".." or relative.startswith(f"..{os.sep}"):
        raise AnalyzerOutputParseError(tool="eslint", detail="path outside scan directory")
    path = original_by_normalized.get(relative, relative)

    findings: list[Finding] = []
    for message in messages:
        try:
            text = message["message"]
            rule_id = message.get("ruleId")
            line = message.get("line")
            level = message.get("severity")
            fatal = message.get("fatal") is True
        except (KeyError, TypeError, AttributeError) as exc:
            raise AnalyzerOutputParseError(tool="eslint", detail=str(exc)) from exc
        if not isinstance(text, str):
            raise AnalyzerOutputParseError(tool="eslint", detail="malformed message")
        if _NOT_A_FINDING.match(text):
            continue
        rule = rule_id if isinstance(rule_id, str) else None
        severity = _severity(rule, level, fatal)
        findings.append(
            Finding(
                file=path,
                line=line if isinstance(line, int) and line >= 1 else None,
                severity=severity,
                message=text,
                source="eslint",
                rule_id=rule,
            )
        )
    return findings


class EslintAnalyzer:
    """``review.static_analysis.StaticAnalyzer`` backed by ``review-eslint``.
    See this module's docstring."""

    name = "eslint"

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]:
        # Vendored dependencies are not the PR's code, and a materialized
        # node_modules/<parser> would shadow the pinned parser ESLint resolves
        # from the scan directory (arbitrary code execution in the Worker).
        content = {p: c for p, c in content.items() if "node_modules" not in _segments(p)}
        if not content:
            # With no paths after `--`, ESLint would lint nothing and print "[]".
            return []
        choice = classify_eslint_config(config_files)
        logger.info(
            "eslint config selected",
            extra={
                "analyzer_name": self.name,
                **choice.outcome.log_fields(),
                **({"config_path": choice.skipped_path} if choice.skipped_path else {}),
            },
        )
        choice.outcome.record(self.name)
        # Only the honored data configs reach disk: an executable config (or one
        # we are skipping) is never written, so it cannot be loaded by accident.
        inert = {name: config_files[name] for name in choice.write_names}
        directory = await materialize_into_temp_directory({**inert, **content})
        try:
            default_name = f".review-eslint-default-{uuid.uuid4().hex}.json"
            await asyncio.to_thread(
                shutil.copyfile, _DEFAULT_PROFILE_PATH, directory / default_name
            )
            original_by_normalized = original_path_mapping(content)
            paths = sorted(original_by_normalized)
            started = time.monotonic()

            try:
                output = await run_analyzer_subprocess(
                    _argv(choice.config_name or default_name, paths), cwd=directory, timeout=timeout
                )
            except AnalyzerInvocationError as exc:
                remaining = timeout - (time.monotonic() - started)
                if (
                    choice.config_name is None
                    or exc.returncode != _ESLINT_CONFIG_REJECTED_EXIT_CODE
                    or remaining <= 0
                ):
                    raise
                logger.warning(
                    "eslint rejected the repo config; re-running with the default profile",
                    extra={
                        "analyzer_name": self.name,
                        "config_source": "default",
                        "config_skip_reason": "rejected_by_tool",
                        "config_path": choice.config_name,
                    },
                )
                ConfigOutcome("default", "rejected_by_tool").record(self.name)
                output = await run_analyzer_subprocess(
                    _argv(default_name, paths), cwd=directory, timeout=remaining
                )
            return [
                finding
                for entry in _parse_output(output)
                for finding in _to_findings(entry, directory, original_by_normalized)
            ]
        finally:
            await cleanup_temp_directory(directory)


_: type[StaticAnalyzer] = EslintAnalyzer
