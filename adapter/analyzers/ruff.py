"""adapter.analyzers.ruff — the concrete ``StaticAnalyzer`` backed by the
pinned ``ruff`` binary (``ruff`` on ``PATH``; the Dockerfile's ``ruff`` stage
installs it with a checksum-verified version, ci.yml mirrors that), per
docs/plans/plan-phase-9.md Phase 3.

**Config is data, never code.** Ruff's config is TOML, so the repo's own
``.ruff.toml`` / ``ruff.toml`` / ``pyproject.toml`` (``[tool.ruff]``) is
honored when ``classify_ruff_config`` (pure, unit-tested) finds nothing that
reads or writes outside the scan directory: ``cache-dir`` and an ``extend``
that is not one of the fetched allow-listed root files make Ruff read or write
arbitrary paths, so such a config is skipped in favour of the pinned default
profile (``ruff_default.toml``). A config Ruff itself rejects (exit 2) is
re-run once with the default profile.

**Exit codes:** ``--exit-zero`` makes "violations found" a success; only a real
failure (exit 2) reaches ``AnalyzerInvocationError``. ``--no-cache`` and
``--no-fix`` keep the run from writing anything but the scratch directory, and
the ``--`` end-of-options guard stops a PR-controlled ``-x.py`` filename being
parsed as a flag.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
import shutil
import time
import tomllib
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

_DEFAULT_PROFILE_PATH = Path(__file__).parent / "ruff_default.toml"

# Root files Ruff itself discovers config from, in its own precedence order.
RUFF_CONFIG_FILENAMES = (".ruff.toml", "ruff.toml", "pyproject.toml")

_MAX_EXTEND_DEPTH = 5
_RUFF_CONFIG_REJECTED_EXIT_CODE = 2

# Ruff has no severity of its own beyond error/warning; the rule-level entries
# rank the correctness classes the default profile exists for.
_RULE_SEVERITY: dict[str, Severity] = {"invalid-syntax": "high", "F821": "high", "F401": "low"}
_SEVERITY_MAP: dict[str, Severity] = {"error": "medium", "warning": "low"}
_DEFAULT_SEVERITY: Severity = "medium"


@dataclass(frozen=True)
class RuffConfigChoice:
    """``config_name`` is the fetched root file to pass to ``--config``, or
    ``None`` to use the pinned default profile."""

    config_name: str | None
    outcome: ConfigOutcome
    skipped_path: str | None = None


def _ruff_settings(name: str, raw: bytes) -> dict[str, Any] | None:
    """The Ruff settings table of a config file; ``None`` when a
    ``pyproject.toml`` has no ``[tool.ruff]`` (not a Ruff config at all)."""
    document = tomllib.loads(raw.decode("utf-8"))
    if name != "pyproject.toml":
        return document
    tool = document.get("tool")
    settings = tool.get("ruff") if isinstance(tool, dict) else None
    return settings if isinstance(settings, dict) else None


def _unsafe_reason(
    name: str, config_files: Mapping[str, bytes], seen: tuple[str, ...] = ()
) -> str | None:
    """Why ``name`` (and anything it ``extend``s) cannot be honored, or
    ``None`` when it is plain, self-contained data."""
    try:
        settings = _ruff_settings(name, config_files[name])
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return "config_invalid"
    if settings is None:
        return "config_invalid" if seen else None
    if "cache-dir" in settings:
        return "cache_dir_not_allowed"
    if "extend" not in settings:
        return None
    target = settings["extend"]
    if not isinstance(target, str):
        return "extend_unresolved"
    resolved = posixpath.normpath(target)
    if (
        resolved not in config_files
        or resolved not in RUFF_CONFIG_FILENAMES
        or resolved in seen
        or resolved == name
        or len(seen) >= _MAX_EXTEND_DEPTH
    ):
        return "extend_unresolved"
    return _unsafe_reason(resolved, config_files, (*seen, name))


def classify_ruff_config(config_files: Mapping[str, bytes]) -> RuffConfigChoice:
    """``_classify_ruff_config`` that treats a pathological config (e.g. nesting
    deep enough to overflow the parser's stack) as invalid, never as a crash."""
    try:
        return _classify_ruff_config(config_files)
    except (RecursionError, TypeError):
        return RuffConfigChoice(None, ConfigOutcome("default", "config_invalid"))


def _classify_ruff_config(config_files: Mapping[str, bytes]) -> RuffConfigChoice:
    """Pure data-only classifier (no I/O): which fetched config, if any, Ruff
    may be pointed at. Mirrors Ruff's own precedence; falls back to the default
    profile (with a ``config_skip_reason``) when the config could touch paths
    outside the scan directory."""
    for name in RUFF_CONFIG_FILENAMES:
        if name not in config_files:
            continue
        try:
            is_ruff_config = _ruff_settings(name, config_files[name]) is not None
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            return RuffConfigChoice(None, ConfigOutcome("default", "config_invalid"), name)
        if not is_ruff_config:
            continue
        reason = _unsafe_reason(name, config_files)
        if reason is not None:
            return RuffConfigChoice(None, ConfigOutcome("default", reason), name)
        return RuffConfigChoice(name, ConfigOutcome("repo"))
    return RuffConfigChoice(None, ConfigOutcome("default"))


def _argv(config_name: str, paths: list[str]) -> list[str]:
    return [
        "ruff",
        "check",
        "--config",
        config_name,
        "--output-format",
        "json",
        # "Violations found" is success; only a real failure exits non-zero.
        "--exit-zero",
        # Nothing but the scratch directory is ever written.
        "--no-cache",
        "--no-fix",
        # Honor the repo's own `exclude` even for explicitly named files.
        "--force-exclude",
        # Keep warnings off the merged stdout+stderr stream we parse as JSON.
        "--quiet",
        "--",
        *paths,
    ]


def _parse_output(raw: bytes) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AnalyzerOutputParseError(tool="ruff", detail=str(exc)) from exc
    if not isinstance(parsed, list):
        raise AnalyzerOutputParseError(tool="ruff", detail="output was not a JSON list")
    return parsed


def _to_finding(
    entry: dict[str, Any], directory: Path, original_by_normalized: dict[str, str]
) -> Finding:
    try:
        filename = entry["filename"]
        row = entry["location"]["row"]
        message = entry["message"]
        code = entry["code"]
    except (KeyError, TypeError) as exc:
        raise AnalyzerOutputParseError(tool="ruff", detail=str(exc)) from exc
    if not isinstance(filename, str) or not isinstance(message, str):
        raise AnalyzerOutputParseError(tool="ruff", detail="malformed entry")

    relative = os.path.relpath(os.path.realpath(filename), os.path.realpath(directory))
    # Segment compare: a file literally named `..x.py` is inside the directory.
    if relative == ".." or relative.startswith(f"..{os.sep}"):
        raise AnalyzerOutputParseError(tool="ruff", detail="path outside scan directory")

    rule_id = code if isinstance(code, str) else None
    raw_severity = entry.get("severity")
    severity = _RULE_SEVERITY.get(rule_id or "") or (
        _SEVERITY_MAP.get(raw_severity, _DEFAULT_SEVERITY)
        if isinstance(raw_severity, str)
        else _DEFAULT_SEVERITY
    )
    return Finding(
        file=original_by_normalized.get(relative, relative),
        line=row if isinstance(row, int) and row >= 1 else None,
        severity=severity,
        message=message,
        source="ruff",
        rule_id=rule_id,
    )


class RuffAnalyzer(ChildEnvironmentMixin):
    """``review.static_analysis.StaticAnalyzer`` backed by the ``ruff`` binary.
    See this module's docstring."""

    name = "ruff"

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]:
        if not content:
            # With no paths after `--`, Ruff would scan the scratch directory.
            return []
        choice = classify_ruff_config(config_files)
        logger.info(
            "ruff config selected",
            extra={
                "analyzer_name": self.name,
                **choice.outcome.log_fields(),
                **({"config_path": choice.skipped_path} if choice.skipped_path else {}),
            },
        )
        choice.outcome.record(self.name)
        directory = await materialize_into_temp_directory({**config_files, **content})
        try:
            default_name = f".review-ruff-default-{uuid.uuid4().hex}.toml"
            await asyncio.to_thread(
                shutil.copyfile, _DEFAULT_PROFILE_PATH, directory / default_name
            )
            original_by_normalized = original_path_mapping(content)
            paths = sorted(original_by_normalized)
            started = time.monotonic()

            try:
                output = await run_analyzer_subprocess(
                    _argv(choice.config_name or default_name, paths),
                    cwd=directory,
                    timeout=timeout,
                    env=self._environ,
                )
            except AnalyzerInvocationError as exc:
                remaining = timeout - (time.monotonic() - started)
                if (
                    choice.config_name is None
                    or exc.returncode != _RUFF_CONFIG_REJECTED_EXIT_CODE
                    or remaining <= 0
                ):
                    raise
                logger.warning(
                    "ruff rejected the repo config; re-running with the default profile",
                    extra={
                        "analyzer_name": self.name,
                        "config_source": "default",
                        "config_skip_reason": "rejected_by_tool",
                        "config_path": choice.config_name,
                    },
                )
                ConfigOutcome("default", "rejected_by_tool").record(self.name)
                output = await run_analyzer_subprocess(
                    _argv(default_name, paths),
                    cwd=directory,
                    timeout=remaining,
                    env=self._environ,
                )
            return [
                _to_finding(entry, directory, original_by_normalized)
                for entry in _parse_output(output)
            ]
        finally:
            await cleanup_temp_directory(directory)


_: type[StaticAnalyzer] = RuffAnalyzer
