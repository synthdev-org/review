"""adapter.analyzers.golangci_lint — the concrete ``StaticAnalyzer`` backed by
the pinned ``golangci-lint`` v2 binary plus a pinned Go toolchain (both on
``PATH``; the Dockerfile's ``go-toolchain`` / ``golangci-tool`` stages install
them into the worker-only ``worker-runtime`` target, ci.yml mirrors that), per
docs/plans/plan-phase-9.md Phase 5 and its Decisions 4. golangci-lint always
loads packages through ``go list``, so the toolchain is not optional.

**Config is data, never code.** ``classify_golangci_config`` (pure,
unit-tested) decides whether the fetched root ``.golangci.{yml,yaml,toml,json}``
may be passed to ``--config``. It is skipped (pinned default profile
``golangci_lint_default.yml`` plus a logged ``config_skip_reason``) when it is
not v2, or when it would load or run repo code (``linters.settings.custom``
plugins, gocritic ``ruleguard`` rules), read a file the PR names
(``template-path``), call out to ``git`` (``issues.new-from-*``), or make the
tool read or write outside the scan directory (``run.*cache*`` and any
``output.formats.*.path`` other than stdout/stderr). A config the tool itself
rejects (exit 3) is re-run once with the default profile.

**Offline, no repo code, no writes outside the scratch directory.** Only the
changed ``.go`` files plus a generated ``go.mod`` are materialized. The
invocation pins ``GOPROXY=off``, ``GOTOOLCHAIN=local``, ``GOFLAGS=-mod=mod``,
``CGO_ENABLED=0`` (nothing compiles C) and puts every cache and temp location
inside a scratch directory in the scan directory. The JSON report is written
to a file there, so a repo config's own text/stdout formats cannot corrupt it.

**Noise (US-A5):** only the changed files exist, so an import of a third-party
module (or of an unchanged sibling package) cannot resolve and an identifier
defined in an unchanged sibling file is "undefined". Those ``typecheck``
findings are dropped. Fidelity limit, by design: golangci-lint skips the
type-aware linters for a package that has any unresolved import, so a package
importing a non-stdlib module yields no lint findings, only (valid) compile
errors.

shortcut: only the repo-root config is read (plan Assumption 9); the generated
``go.mod`` carries the pinned toolchain's language version, not the repo's;
a ``go.mod``/``go.work``/``vendor`` of the repo is never used. The deny-list of
config keys below covers v2.15; re-check it when bumping golangci-lint.
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
import tomllib
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

_DEFAULT_PROFILE_PATH = Path(__file__).parent / "golangci_lint_default.yml"

# golangci-lint's own discovery order for a project config.
GOLANGCI_CONFIG_FILENAMES = (
    ".golangci.yml",
    ".golangci.yaml",
    ".golangci.toml",
    ".golangci.json",
)

# The language version of the generated go.mod: the pinned toolchain's own, so
# every syntax it accepts (generics, range-over-func) type-checks.
_GO_MOD = b"module review.local/scan\n\ngo 1.27\n"

_CONFIG_REJECTED_EXIT_CODE = 3
_NO_GO_FILES_EXIT_CODE = 5

# Keys that load or run repo code, anywhere in the document.
_EXECUTABLE_KEYS = frozenset({"ruleguard"})
# Keys that read a path the PR names or shell out to git.
_UNSUPPORTED_KEYS = frozenset(
    {
        "template-path",
        "new-from-rev",
        "new-from-patch",
        "new-from-merge-base",
        "new-from-rev-file",
        "new",
        "whole-files",
    }
)
_SAFE_OUTPUT_PATHS = frozenset({"", "stdout", "stderr"})

_LINTER_SEVERITY: dict[str, Severity] = {
    "typecheck": "high",
    "govet": "medium",
    "staticcheck": "medium",
    "errcheck": "medium",
    "ineffassign": "low",
}
_DEFAULT_SEVERITY: Severity = "medium"

_STATICCHECK_CODE = re.compile(r"^([A-Z]{1,3}[0-9]{3,4}): ")
# A compile failure comes back as ``: # pkg\n./f.go:3:4: msg\n...``.
_BUILD_ERROR_LINE = re.compile(r"^\.?/?(?P<file>[^\s:][^:]*\.go):(?P<line>\d+):\d+: (?P<msg>.+)$")
_COULD_NOT_IMPORT = re.compile(r"^could not import (\S+) ")
# Only the changed files exist, so these mean "a dependency or a sibling file is
# missing", never "a bug in the change".
_UNRESOLVED_MARKERS = (
    "undefined: ",
    " undefined (type ",
    "cannot find module providing package",
    "no required module provides package",
    "cannot find package",
)


@dataclass(frozen=True)
class GolangciConfigChoice:
    """``config_name`` is the fetched root file to pass to ``--config``, or
    ``None`` to use the pinned default profile."""

    config_name: str | None
    outcome: ConfigOutcome
    skipped_path: str | None = None


def _parse_document(name: str, raw: bytes) -> object:
    text = raw.decode("utf-8")
    if name.endswith(".toml"):
        return tomllib.loads(text)
    if name.endswith(".json"):
        return json.loads(text)
    return yaml.safe_load(text)


def _lower_keys(node: object, memo: dict[int, object] | None = None) -> object:
    """golangci-lint (viper) reads keys case-insensitively, so the safety checks
    must too: ``Output: {Formats: ...}`` is the same config as ``output:``.
    Memoized by identity so a YAML alias DAG ("billion laughs") is walked once."""
    memo = {} if memo is None else memo
    if id(node) in memo:
        return memo[id(node)]
    if isinstance(node, dict):
        lowered: dict[object, object] = {}
        memo[id(node)] = lowered
        for k, v in node.items():
            lowered[k.lower() if isinstance(k, str) else k] = _lower_keys(v, memo)
        return lowered
    if isinstance(node, list):
        items: list[object] = []
        memo[id(node)] = items
        items.extend(_lower_keys(v, memo) for v in node)
        return items
    return node


def _walk_keys(node: object) -> set[str]:
    """Every mapping key anywhere in the document."""
    keys: set[str] = set()
    seen: set[int] = set()
    pending = [node]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, dict):
            keys.update(k for k in current if isinstance(k, str))
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return keys


def _unsafe_reason(document: object) -> str | None:
    """Why the parsed config cannot be honored, or ``None`` when it is plain data."""
    if not isinstance(document, dict):
        return "config_invalid"
    if str(document.get("version")) != "2":
        return "unsupported_config_version"
    keys = _walk_keys(document)
    linters = document.get("linters")
    settings = linters.get("settings") if isinstance(linters, dict) else None
    if (isinstance(settings, dict) and "custom" in settings) or keys & _EXECUTABLE_KEYS:
        return "executable_config"
    if keys & _UNSUPPORTED_KEYS:
        return "unsupported_option"
    run = document.get("run")
    if isinstance(run, dict) and any("cache" in str(k).lower() for k in run):
        return "cache_dir_not_allowed"
    output = document.get("output")
    formats = output.get("formats") if isinstance(output, dict) else None
    if formats is not None:
        if not isinstance(formats, dict):
            return "unsupported_syntax"
        for entry in formats.values():
            if isinstance(entry, dict) and entry.get("path") not in (*_SAFE_OUTPUT_PATHS, None):
                return "output_path_not_allowed"
    return None


def _classify_golangci_config(config_files: Mapping[str, bytes]) -> GolangciConfigChoice:
    for name in GOLANGCI_CONFIG_FILENAMES:
        if name not in config_files:
            continue
        try:
            document = _parse_document(name, config_files[name])
        except (UnicodeDecodeError, ValueError, yaml.YAMLError):
            return GolangciConfigChoice(None, ConfigOutcome("default", "config_invalid"), name)
        reason = _unsafe_reason(_lower_keys(document))
        if reason is not None:
            return GolangciConfigChoice(None, ConfigOutcome("default", reason), name)
        return GolangciConfigChoice(name, ConfigOutcome("repo"))
    return GolangciConfigChoice(None, ConfigOutcome("default"))


def classify_golangci_config(config_files: Mapping[str, bytes]) -> GolangciConfigChoice:
    """Pure data-only classifier (no I/O): which fetched config, if any,
    golangci-lint may be pointed at. Mirrors its own precedence; a pathological
    document (e.g. nesting deep enough to overflow the parser) is invalid, never
    a crash."""
    try:
        return _classify_golangci_config(config_files)
    except (RecursionError, TypeError):
        return GolangciConfigChoice(None, ConfigOutcome("default", "config_invalid"))


def _argv(config_name: str, report_name: str) -> list[str]:
    return [
        "golangci-lint",
        "run",
        "--config",
        config_name,
        # The report goes to a file so a repo-chosen stdout format cannot mix into it.
        "--output.json.path",
        report_name,
        "--show-stats=false",
        # "Issues found" is success; only a real failure exits non-zero.
        "--issues-exit-code=0",
        "--max-issues-per-linter=0",
        "--max-same-issues=0",
        "--concurrency=2",
        # The lock file this replaces would serialize Workers' runs; TMPDIR is
        # private to the invocation anyway.
        "--allow-parallel-runners",
        "--modules-download-mode=mod",
        "./...",
    ]


def _scratch_env(scratch: Path) -> dict[str, str]:
    """Pins the toolchain offline and every cache/temp location inside ``scratch``."""
    return {
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOTOOLCHAIN": "local",
        "GOFLAGS": "-mod=mod -buildvcs=false -modcacherw",
        "GOWORK": "off",
        "GOENV": "off",
        "GOTELEMETRY": "off",
        "GOVCS": "*:off",
        "GO111MODULE": "on",
        "CGO_ENABLED": "0",
        "GOCACHE": str(scratch / "gocache"),
        "GOMODCACHE": str(scratch / "gomod"),
        "GOPATH": str(scratch / "gopath"),
        "GOTMPDIR": str(scratch / "tmp"),
        "TMPDIR": str(scratch / "tmp"),
        "HOME": str(scratch / "home"),
        "XDG_CACHE_HOME": str(scratch / "cache"),
        "GOLANGCI_LINT_CACHE": str(scratch / "lint"),
    }


def _make_scratch(scratch: Path) -> None:
    for sub in ("gocache", "gomod", "gopath", "tmp", "home", "cache", "lint"):
        (scratch / sub).mkdir(parents=True)


def _parse_report(raw: bytes) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AnalyzerOutputParseError(tool="golangci-lint", detail=str(exc)) from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("Issues", []) or [], list):
        raise AnalyzerOutputParseError(tool="golangci-lint", detail="unexpected report shape")
    return parsed.get("Issues") or []


def _is_unresolved_dependency(linter: str, text: str) -> bool:
    if linter != "typecheck":
        return False
    imported = _COULD_NOT_IMPORT.match(text)
    if imported is not None:
        # A stdlib import has no dot in its first element; failing to resolve one
        # means a broken toolchain, which must stay visible.
        return "." in imported.group(1).split("/", 1)[0]
    return any(marker in text for marker in _UNRESOLVED_MARKERS)


def _relative(filename: str, directory: Path) -> str:
    relative = os.path.relpath(os.path.realpath(directory / filename), os.path.realpath(directory))
    # Segment compare: a file literally named `..x.go` is inside the directory.
    if relative == ".." or relative.startswith(f"..{os.sep}"):
        raise AnalyzerOutputParseError(tool="golangci-lint", detail="path outside scan directory")
    return relative


def _to_findings(
    entry: dict[str, Any], directory: Path, original_by_normalized: dict[str, str]
) -> list[Finding]:
    try:
        linter = entry["FromLinter"]
        text = entry["Text"]
        position = entry["Pos"]
        filename = position["Filename"]
        line = position["Line"]
    except (KeyError, TypeError) as exc:
        raise AnalyzerOutputParseError(tool="golangci-lint", detail=str(exc)) from exc
    if not all(isinstance(v, str) for v in (linter, text, filename)):
        raise AnalyzerOutputParseError(tool="golangci-lint", detail="malformed entry")

    located: list[tuple[str, int | None, str]]
    if linter == "typecheck" and text.startswith(": # "):
        # One issue carries the whole package's compile errors; split them so each
        # lands on its own line (and the changed-lines filter can judge it).
        located = [
            (m["file"], int(m["line"]), m["msg"])
            for m in map(_BUILD_ERROR_LINE.match, text.splitlines()[1:])
            if m is not None
        ]
    else:
        located = [(filename, line if isinstance(line, int) and line >= 1 else None, text)]

    findings: list[Finding] = []
    for path, row, message in located:
        if _is_unresolved_dependency(linter, message):
            continue
        try:
            relative = _relative(path, directory)
        except AnalyzerOutputParseError:
            # A path quoted inside a compile message (attacker-influenced source) must
            # not abort the whole run; drop just that line.
            if linter == "typecheck" and text.startswith(": # "):
                continue
            raise
        code = _STATICCHECK_CODE.match(message) if linter == "staticcheck" else None
        findings.append(
            Finding(
                file=original_by_normalized.get(relative, relative),
                line=row,
                severity=_LINTER_SEVERITY.get(linter, _DEFAULT_SEVERITY),
                message=message,
                source="golangci_lint",
                rule_id=code.group(1) if code else linter,
            )
        )
    return findings


class GolangciLintAnalyzer:
    """``review.static_analysis.StaticAnalyzer`` backed by ``golangci-lint``.
    See this module's docstring."""

    name = "golangci_lint"

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]:
        # The go command rejects a file named `-x.go` ("invalid input file name"),
        # which would fail its whole package, so such a file is not linted.
        # Paths under the reserved scratch prefix would pre-create (or collide with)
        # our scratch directories, so they are not linted either.
        content = {
            p: c
            for p, c in content.items()
            if not p.rsplit("/", 1)[-1].startswith("-")
            and not posixpath.normpath(p.replace("\\", "/")).startswith(".review-golangci")
        }
        if not content:
            return []
        choice = classify_golangci_config(config_files)
        logger.info(
            "golangci-lint config selected",
            extra={
                "analyzer_name": self.name,
                **choice.outcome.log_fields(),
                **({"config_path": choice.skipped_path} if choice.skipped_path else {}),
            },
        )
        choice.outcome.record(self.name)
        # Only an honored data config reaches disk; the generated go.mod comes last
        # so nothing in the PR can replace it.
        inert = {choice.config_name: config_files[choice.config_name]} if choice.config_name else {}
        directory = await materialize_into_temp_directory({**inert, **content, "go.mod": _GO_MOD})
        try:
            scratch = directory / ".review-golangci"
            await asyncio.to_thread(_make_scratch, scratch)
            default_name = f".review-golangci-default-{uuid.uuid4().hex}.yml"
            await asyncio.to_thread(
                shutil.copyfile, _DEFAULT_PROFILE_PATH, directory / default_name
            )
            original_by_normalized = original_path_mapping(content)
            env = _scratch_env(scratch)
            started = time.monotonic()

            async def scan(config: str, budget: float) -> bytes:
                report = f".review-golangci/report-{uuid.uuid4().hex}.json"
                await run_analyzer_subprocess(
                    _argv(config, report), cwd=directory, timeout=budget, env_overrides=env
                )
                return await asyncio.to_thread((directory / report).read_bytes)

            try:
                try:
                    raw = await scan(choice.config_name or default_name, timeout)
                except AnalyzerInvocationError as exc:
                    remaining = timeout - (time.monotonic() - started)
                    if (
                        choice.config_name is None
                        or exc.returncode != _CONFIG_REJECTED_EXIT_CODE
                        or remaining <= 0
                    ):
                        raise
                    logger.warning(
                        "golangci-lint rejected the repo config; re-running with the default"
                        " profile",
                        extra={
                            "analyzer_name": self.name,
                            "config_source": "default",
                            "config_skip_reason": "rejected_by_tool",
                            "config_path": choice.config_name,
                        },
                    )
                    ConfigOutcome("default", "rejected_by_tool").record(self.name)
                    raw = await scan(default_name, remaining)
            except AnalyzerInvocationError as exc:
                if exc.returncode == _NO_GO_FILES_EXIT_CODE:
                    # Every changed file sits in a directory Go ignores (testdata, _x, .x)
                    # or is excluded by a build constraint: nothing to lint.
                    return []
                raise
            except FileNotFoundError as exc:
                raise AnalyzerOutputParseError(
                    tool="golangci-lint", detail="no report was written"
                ) from exc
            return [
                finding
                for entry in _parse_report(raw)
                for finding in _to_findings(entry, directory, original_by_normalized)
            ]
        finally:
            await cleanup_temp_directory(directory)


_: type[StaticAnalyzer] = GolangciLintAnalyzer
