"""adapter.analyzers.phpstan — the concrete ``StaticAnalyzer`` backed by the
pinned PHPStan phar (``review-phpstan`` on ``PATH``: a minimal PHP CLI built
from source plus a checksum-verified ``phpstan.phar``; the Dockerfile's
``php-runtime`` / ``phpstan-tool`` stages install it, ci.yml mirrors that), per
docs/plans/plan-phase-9.md Phase 7. There is no composer and no autoloader.

**Config is data, never code.** A ``phpstan.neon`` can execute code
(``bootstrapFiles``), load project code (``scanFiles``/``scanDirectories``,
autoload directives) or instantiate arbitrary services (``includes``,
``services``, ``extensions``, ``rules``). ``classify_phpstan_config`` (pure,
unit-tested) therefore uses an *allow-list*: the file may hold only a
``parameters:`` block whose keys are all in ``_ALLOWED_PARAMETERS``; anything
else skips the config in favour of the pinned default profile
(``phpstan_default.neon``) with a logged ``config_skip_reason``. A config
PHPStan itself rejects (exit 1 with no JSON report) is re-run once with the
default profile.

**Isolation:** the chosen config is wrapped in a generated file that includes
it and pins ``tmpDir`` inside the scan directory and single-process analysis
(no child PHP processes); the explicit ``-c`` stops any auto-discovery. Any
changed ``vendor/`` file is dropped before materialization, because PHPStan
auto-requires ``vendor/autoload.php`` from its working directory. The ``--``
guard stops a PR-controlled ``-x.php`` filename being parsed as a flag.

**Noise (US-A5):** without ``vendor/`` every unresolved class/function/constant
is a missing dependency, not a bug in the change; those findings are dropped.

shortcut: only the repo-root config is read (plan Assumption 9) and ``includes``
(e.g. a baseline file) is not honored; add allow-listed include support if
pilot repos hit it. The neon scan is line/indent based, so an exotic but legal
layout is skipped as ``unsupported_syntax`` rather than parsed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adapter.analyzers.environment import ChildEnvironmentMixin
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

_DEFAULT_PROFILE_PATH = Path(__file__).parent / "phpstan_default.neon"

# PHPStan's own discovery order for a project config.
PHPSTAN_CONFIG_FILENAMES = ("phpstan.neon", "phpstan.neon.dist", "phpstan.dist.neon")

# Parameters that are plain data (rule strictness switches, ignore lists,
# excludes). Everything else - above all bootstrapFiles, scanFiles,
# scanDirectories, autoload_files, autoload_directories, tmpDir, stubFiles,
# parallel - is refused.
_ALLOWED_PARAMETERS = frozenset(
    {
        "level",
        "paths",
        "excludePaths",
        "ignoreErrors",
        "reportUnmatchedIgnoredErrors",
        "treatPhpDocTypesAsCertain",
        "phpVersion",
        "checkMissingCallableSignature",
        "checkTooWideReturnTypesInProtectedAndPublicMethods",
        "checkUninitializedProperties",
        "checkDynamicProperties",
        "checkExplicitMixed",
        "checkImplicitMixed",
        "checkBenevolentUnionTypes",
        "checkUnionTypes",
        "checkNullables",
        "checkThisOnly",
        "checkMissingIterableValueType",
        "checkGenericClassInNonGenericObjectType",
        "reportMaybesInMethodSignatures",
        "reportMaybesInPropertyPhpDocTypes",
        "reportStaticMethodSignatures",
        "reportAlwaysTrueInLastCondition",
        "polluteScopeWithLoopInitialAssignments",
        "polluteScopeWithAlwaysIterableForeach",
        "inferPrivatePropertyTypeFromConstructor",
    }
)
# Keys that load or run code or services; reported distinctly from merely unknown keys.
_CODE_LOADING_KEYS = frozenset(
    {
        "bootstrapFiles",
        "scanFiles",
        "scanDirectories",
        "autoload_files",
        "autoload_directories",
        "includes",
        "services",
        "extensions",
        "rules",
        "conditionalTags",
        "parametersSchema",
        "stubFiles",
    }
)

_PARAMETERS_LINE = re.compile(r"^parameters\s*:\s*(#.*)?$")
_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:(\s|$)")

_PHPSTAN_CONFIG_REJECTED_EXIT_CODE = 1

_IDENTIFIER_SEVERITY: dict[str, Severity] = {"phpstan.parse": "high", "variable.undefined": "high"}
_DEFAULT_SEVERITY: Severity = "medium"

# A missing dependency (no vendor/ is ever fetched), not a defect in the change.
_UNRESOLVED_IDENTIFIERS = frozenset(
    {
        "class.notFound",
        "interface.notFound",
        "trait.notFound",
        "enum.notFound",
        "function.notFound",
        "constant.notFound",
        "attribute.notFound",
    }
)
_UNRESOLVED_MESSAGE = re.compile(
    r"^(Instantiated class|Class|Interface|Trait|Enum|Function|Constant|Attribute class) "
    r"\S+ not found\.$"
    r"|extends unknown class|implements unknown interface|uses unknown trait|on an unknown class"
)


@dataclass(frozen=True)
class PhpstanConfigChoice:
    """``config_name`` is the fetched root file to include, or ``None`` to use
    the pinned default profile. ``sets_level`` says whether the chosen config
    has its own ``level`` (otherwise the adapter supplies the default one)."""

    config_name: str | None
    outcome: ConfigOutcome
    skipped_path: str | None = None
    sets_level: bool = False


def _unsafe_reason(raw: bytes) -> tuple[str | None, bool]:
    """``(reason, sets_level)``: why this neon file cannot be honored (``None``
    when it is only an allow-listed ``parameters`` block) and whether it sets
    ``level``."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "config_invalid", False
    # NEON decodes ``\uXXXX`` in strings, which can spell ``%env.X%`` past a plain match.
    if "%env." in text or "\\u" in text:
        return "env_reference", False
    seen_parameters = False
    child_indent: str | None = None
    sets_level = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = line[: len(line) - len(line.lstrip(" \t"))]
        if not indent:
            if not _PARAMETERS_LINE.match(line):
                key = _KEY_LINE.match(line)
                if key and key.group(1) in _CODE_LOADING_KEYS:
                    return "executable_config", False
                return "unsupported_option", False
            seen_parameters = True
            child_indent = None
            continue
        if not seen_parameters:
            return "unsupported_syntax", False
        if child_indent is None:
            child_indent = indent
        if indent == child_indent:
            key = _KEY_LINE.match(stripped)
            if key is None:
                return "unsupported_syntax", False
            name = key.group(1)
            if name in _CODE_LOADING_KEYS:
                return "executable_config", False
            if name not in _ALLOWED_PARAMETERS:
                return "unsupported_option", False
            sets_level = sets_level or name == "level"
        elif not indent.startswith(child_indent):
            return "unsupported_syntax", False
    return (None, sets_level) if seen_parameters else ("config_invalid", False)


def classify_phpstan_config(config_files: Mapping[str, bytes]) -> PhpstanConfigChoice:
    """Pure data-only classifier (no I/O): which fetched config, if any,
    PHPStan may be pointed at. Mirrors PHPStan's own precedence; falls back to
    the default profile (with a ``config_skip_reason``) when the config could
    load or run code."""
    for name in PHPSTAN_CONFIG_FILENAMES:
        if name not in config_files:
            continue
        reason, sets_level = _unsafe_reason(config_files[name])
        if reason is not None:
            return PhpstanConfigChoice(None, ConfigOutcome("default", reason), name)
        return PhpstanConfigChoice(name, ConfigOutcome("repo"), sets_level=sets_level)
    return PhpstanConfigChoice(None, ConfigOutcome("default"))


def _wrapper_config(directory: Path, included: str, sets_level: bool) -> str:
    """The generated config PHPStan is pointed at: it includes the chosen
    (data-only) config and pins what a repo config must not control."""
    lines = [
        "includes:",
        f"    - {json.dumps(str(directory / included))}",
        "parameters:",
        f"    tmpDir: {json.dumps(str(directory / '.phpstan-tmp'))}",
        "    parallel:",
        "        maximumNumberOfProcesses: 1",
    ]
    if not sets_level:
        lines.append("    level: 1")
    return "\n".join(lines) + "\n"


def _argv(config_name: str, paths: list[str]) -> list[str]:
    return [
        "review-phpstan",
        "analyse",
        "--configuration",
        config_name,
        "--error-format",
        "json",
        "--no-progress",
        "--no-interaction",
        "--",
        *paths,
    ]


def _parse_output(raw: bytes) -> dict[str, Any]:
    # PHPStan can append a human-readable notice after the JSON report, so decode
    # the leading object only.
    try:
        text = raw.decode("utf-8").lstrip()
        parsed, _ = json.JSONDecoder().raw_decode(text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise AnalyzerOutputParseError(tool="phpstan", detail=str(exc)) from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("files", {}), dict):
        raise AnalyzerOutputParseError(tool="phpstan", detail="unexpected report shape")
    return parsed


def _is_unresolved_dependency(message: str, identifier: object) -> bool:
    return (isinstance(identifier, str) and identifier in _UNRESOLVED_IDENTIFIERS) or bool(
        _UNRESOLVED_MESSAGE.search(message)
    )


def _to_findings(
    filename: str,
    entry: object,
    directory: Path,
    original_by_normalized: dict[str, str],
) -> list[Finding]:
    relative = os.path.relpath(os.path.realpath(filename), os.path.realpath(directory))
    # Segment compare: a file literally named `..x.php` is inside the directory.
    if relative == ".." or relative.startswith(f"..{os.sep}"):
        raise AnalyzerOutputParseError(tool="phpstan", detail="path outside scan directory")
    if not isinstance(entry, dict) or not isinstance(entry.get("messages"), list):
        raise AnalyzerOutputParseError(tool="phpstan", detail="malformed entry")

    findings: list[Finding] = []
    for message in entry["messages"]:
        try:
            text = message["message"]
            line = message.get("line")
            identifier = message.get("identifier")
        except (KeyError, TypeError, AttributeError) as exc:
            raise AnalyzerOutputParseError(tool="phpstan", detail=str(exc)) from exc
        if not isinstance(text, str):
            raise AnalyzerOutputParseError(tool="phpstan", detail="malformed message")
        if _is_unresolved_dependency(text, identifier):
            continue
        rule_id = identifier if isinstance(identifier, str) else None
        findings.append(
            Finding(
                file=original_by_normalized.get(relative, relative),
                line=line if isinstance(line, int) and line >= 1 else None,
                severity=_IDENTIFIER_SEVERITY.get(rule_id or "", _DEFAULT_SEVERITY),
                message=text,
                source="phpstan",
                rule_id=rule_id,
            )
        )
    return findings


def _segments(path: str) -> list[str]:
    return path.replace("\\", "/").lower().split("/")


class PhpstanAnalyzer(ChildEnvironmentMixin):
    """``review.static_analysis.StaticAnalyzer`` backed by ``review-phpstan``.
    See this module's docstring."""

    name = "phpstan"

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]:
        # Vendored dependencies are not the PR's code, and PHPStan requires
        # vendor/autoload.php from its working directory (arbitrary code
        # execution in the Worker if the PR supplies one).
        content = {p: c for p, c in content.items() if "vendor" not in _segments(p)}
        if not content:
            # With no paths after `--`, PHPStan would analyse the configured paths.
            return []
        choice = classify_phpstan_config(config_files)
        logger.info(
            "phpstan config selected",
            extra={
                "analyzer_name": self.name,
                **choice.outcome.log_fields(),
                **({"config_path": choice.skipped_path} if choice.skipped_path else {}),
            },
        )
        choice.outcome.record(self.name)
        # Only an honored data config reaches disk: a skipped one (possibly
        # executable) is never written, so it cannot be loaded by accident.
        inert = {choice.config_name: config_files[choice.config_name]} if choice.config_name else {}
        directory = await materialize_into_temp_directory({**inert, **content})
        try:
            suffix = uuid.uuid4().hex
            default_name = f".review-phpstan-default-{suffix}.neon"
            await asyncio.to_thread(
                shutil.copyfile, _DEFAULT_PROFILE_PATH, directory / default_name
            )
            original_by_normalized = original_path_mapping(content)
            paths = sorted(original_by_normalized)

            async def wrapper(included: str, sets_level: bool) -> str:
                name = f".review-phpstan-{uuid.uuid4().hex}.neon"
                body = _wrapper_config(directory, included, sets_level)
                await asyncio.to_thread((directory / name).write_text, body)
                return name

            started = time.monotonic()
            try:
                config = await wrapper(choice.config_name or default_name, choice.sets_level)
                output = await run_analyzer_subprocess(
                    _argv(config, paths), cwd=directory, timeout=timeout, env=self._environ
                )
            except AnalyzerInvocationError as exc:
                remaining = timeout - (time.monotonic() - started)
                if (
                    choice.config_name is None
                    or exc.returncode != _PHPSTAN_CONFIG_REJECTED_EXIT_CODE
                    or remaining <= 0
                ):
                    raise
                logger.warning(
                    "phpstan rejected the repo config; re-running with the default profile",
                    extra={
                        "analyzer_name": self.name,
                        "config_source": "default",
                        "config_skip_reason": "rejected_by_tool",
                        "config_path": choice.config_name,
                    },
                )
                ConfigOutcome("default", "rejected_by_tool").record(self.name)
                output = await run_analyzer_subprocess(
                    _argv(await wrapper(default_name, False), paths),
                    cwd=directory,
                    timeout=remaining,
                    env=self._environ,
                )
            report = _parse_output(output)
            return [
                finding
                for filename, entry in report.get("files", {}).items()
                for finding in _to_findings(filename, entry, directory, original_by_normalized)
            ]
        finally:
            await cleanup_temp_directory(directory)


_: type[StaticAnalyzer] = PhpstanAnalyzer
