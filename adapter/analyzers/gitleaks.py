"""adapter.analyzers.gitleaks — the concrete ``StaticAnalyzer`` and
``TextRedactor`` implementations backed by the vendored Gitleaks binary, per
the plan's Phase 5 section.

Two distinct roles, both backed by the same vendored ``gitleaks`` binary
(vendored into the runtime/dev image stages per the plan's Assumptions
section — see the Dockerfile — and installed on CI's integration-test
runner separately, since that job runs ``pytest -m integration`` directly
on the runner, not inside the built image):

- ``GitleaksAnalyzer`` (``review.static_analysis.StaticAnalyzer``): scans
  every materialized file in one invocation (``detect --source . --no-git``)
  and normalizes each match into a ``findings.models.Finding``.
- ``GitleaksTextRedactor`` (``findings.redact.TextRedactor``): a second,
  independent use of the same binary — scans one scratch file holding a
  single candidate finding's own text and returns it with any
  Gitleaks-detectable secret redacted. Reused by both this step
  (``review/static_analysis.py``, for a Semgrep finding's own free-text
  fields) and Phase 6's llm-review step.

Both invocations pass ``--redact`` so a matched secret's actual value never
appears in Gitleaks' own JSON output — see the plan's Phase 5 section for
why this matters beyond normal finding hygiene (dry-run mode logs the
would-be finding payload at ``info``, and an unredacted secret value would
otherwise land in the platform's own structured logs).

**Gitleaks' own JSON output goes to a ``--report-path`` file, not stdout —
a factual gap in the plan's own generalized "chunked-read of process.stdout"
description, resolved here as the smallest reasonable deviation, not a
silent one:** the plan's Phase 5 section describes bounding "Gitleaks' and
Semgrep's JSON output" via ``adapter/analyzers/subprocess_runner.py``'s
chunked-stdout-read cap uniformly, matching Semgrep's own ``--json``-to-
stdout behavior (``adapter/analyzers/semgrep.py``) — but Gitleaks' CLI has
no stdout-JSON mode; ``--report-path`` writing to a real file is its only
supported output mechanism. This module still routes the invocation itself
through ``run_analyzer_subprocess`` (getting the same kill-and-reap-on-
timeout/cancellation and bounded-stdout-drain safety properties for the
process's own progress/log output on stdout+stderr, and catching a genuine
non-zero-exit tool failure the same way every other analyzer invocation
does), then separately reads the report file it wrote — applying the same
``max_output_bytes`` cap to that read (via ``Path.stat().st_size`` checked
before reading, not an unbounded ``read_text()``) for the same
pathological-output-size protection the stdout cap gives Semgrep, rather
than trusting the file is small just because ordinary tool output usually
is.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from adapter.analyzers.environment import ChildEnvironmentMixin
from adapter.analyzers.exceptions import AnalyzerOutputParseError
from adapter.analyzers.subprocess_runner import (
    cleanup_temp_directory,
    materialize_into_temp_directory,
    original_path_mapping,
    run_analyzer_subprocess,
    validate_relative_path,
)
from findings.models import Finding, Severity
from findings.redact import TextRedactor
from ingestion.diff import Diff
from review.static_analysis import StaticAnalyzer

logger = logging.getLogger(__name__)

_REPORT_FILENAME = ".review-gitleaks-report.json"
_SELF_CONFIG_FILES = frozenset({".gitleaks.toml", ".gitleaksignore"})
_SCANNED_PREFIX = "pr-config-"
# An inline ``gitleaks:allow`` makes gitleaks skip its line; in the diff under review that is
# attacker-controlled, so local mode defuses it in the scanned copy (``ignore_pr_config=True``; the
# server-mode default honours a repo's own allowlists), same length so lines hold.
_ALLOW_MARKER = re.compile(rb"gitleaks:allow", re.IGNORECASE)


def _defuse_allow_markers(data: bytes) -> bytes:
    return _ALLOW_MARKER.sub(lambda m: b"x" * len(m.group(0)), data)


_REDACTION_SCRATCH_FILENAME = "candidate.txt"
_REDACTION_MARKER = "[REDACTED]"
# Gitleaks has no severity concept of its own — a matched secret is
# uniformly treated as this fixed value. See findings.models.Severity's own
# docstring for why every producer normalizes into this shared vocabulary.
_GITLEAKS_SEVERITY: Severity = "critical"
# Matches adapter/analyzers/subprocess_runner.py's own default — the report
# file this module reads is a second output surface for the same tool
# invocation, deserving the same pathological-size protection.
_MAX_REPORT_BYTES = 10_000_000


def _argv(report_path: Path) -> list[str]:
    return [
        "gitleaks",
        "detect",
        "--source",
        ".",
        "--no-git",
        "--redact",
        "--report-format",
        "json",
        "--report-path",
        str(report_path),
        # Gitleaks' own default exit code (1) means "leaks found" — this
        # platform treats "ran to completion, with or without findings" as
        # the only success signal (findings are a normal, expected outcome,
        # not a tool failure), so --exit-code 0 makes that the literal
        # process exit code; any other exit code is a genuine tool failure
        # (bad invocation, a panic), handled the same way as any other
        # analyzer's non-zero exit.
        "--exit-code",
        "0",
    ]


def _read_report(report_path: Path) -> list[dict[str, Any]]:
    try:
        size = report_path.stat().st_size
    except OSError as exc:
        raise AnalyzerOutputParseError(tool="gitleaks", detail=str(exc)) from exc
    if size > _MAX_REPORT_BYTES:
        logger.error(
            "analyzer output exceeded the size cap",
            extra={"argv": ["gitleaks", "detect"], "max_output_bytes": _MAX_REPORT_BYTES},
        )
        raise AnalyzerOutputParseError(
            tool="gitleaks",
            detail=f"report file is {size} bytes, over the {_MAX_REPORT_BYTES}-byte cap",
        )
    try:
        parsed = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AnalyzerOutputParseError(tool="gitleaks", detail=str(exc)) from exc
    if not isinstance(parsed, list):
        raise AnalyzerOutputParseError(tool="gitleaks", detail="report was not a JSON list")
    return parsed


def _to_finding(entry: dict[str, Any], original_by_normalized: dict[str, str]) -> Finding:
    try:
        normalized_file = entry["File"]
    except (KeyError, TypeError) as exc:
        raise AnalyzerOutputParseError(tool="gitleaks", detail=str(exc)) from exc
    # Gitleaks reports the (already-normalized) path it actually scanned —
    # map it back to the original GitHub-provided path (the `content` dict's
    # own key), same as adapter/analyzers/semgrep.py's `_to_finding`, so a
    # posted review comment lands on a path GitHub's PR review API
    # recognizes even when the original path contained a backslash.
    file = original_by_normalized.get(normalized_file, normalized_file)
    rule_id = entry.get("RuleID")
    description = entry.get("Description") or "secret detected"
    line = entry.get("StartLine")
    return Finding(
        file=file,
        line=line if isinstance(line, int) else None,
        severity=_GITLEAKS_SEVERITY,
        message=f"{rule_id}: {description}" if rule_id else description,
        source="gitleaks",
        rule_id=rule_id if isinstance(rule_id, str) else None,
    )


def _redact_matched_lines(text: str, entries: list[dict[str, Any]]) -> str:
    """Replaces every line touched by at least one Gitleaks match (by its
    ``StartLine``..``EndLine`` span) with a fixed marker.

    **Deliberately whole-line, not column-precise, per this module's own
    fail-closed design:** Gitleaks' per-entry ``StartColumn``/``EndColumn``
    describe the match's exact span, but splicing only that span out
    correctly requires getting 1-indexed/0-indexed column arithmetic exactly
    right — an off-by-one there would leave a partial secret character
    exposed, a redaction *bug*, not a cosmetic one. Redacting the entire
    line a match was found on is coarser (it also drops any non-secret
    context sharing that line) but categorically safer: this function's own
    input is normally a short finding message, not a file an operator needs
    full surrounding context for, and this whole mechanism's own fail-closed
    posture (an unredacted secret is a materially worse outcome than an
    over-redacted message) favors the unambiguously-safe approach.
    """
    lines = text.split("\n")
    touched: set[int] = set()
    for entry in entries:
        start_line = entry.get("StartLine")
        if not isinstance(start_line, int):
            continue
        end_line = entry.get("EndLine")
        end_line = end_line if isinstance(end_line, int) else start_line
        for line_number in range(start_line, end_line + 1):
            touched.add(line_number - 1)
    return "\n".join(
        _REDACTION_MARKER if index in touched else line for index, line in enumerate(lines)
    )


async def _run_gitleaks(
    content: dict[str, bytes],
    *,
    timeout: float,
    ignore_pr_config: bool = False,
    environ: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Shared materialize -> invoke -> read-report -> cleanup sequence used
    by both ``GitleaksAnalyzer.run`` and ``GitleaksTextRedactor.redact`` —
    the only difference between the two call sites is what's materialized
    (the diff's real files vs. one scratch file holding candidate text) and
    what each does with the returned entries.
    """
    # Gitleaks auto-loads these from the source root; a PR must not be able to switch the scan off.
    # They are still scanned (a PR can add a real secret to one), under a name it does not load.
    # Off by default (server mode honours a repo's own config); local mode passes True.
    renamed: dict[str, str] = {}
    if ignore_pr_config:
        content = {path: _defuse_allow_markers(data) for path, data in content.items()}
        for path in list(content):
            # Lower-cased: on a case-insensitive filesystem (macOS, Windows) ``.Gitleaks.toml`` is
            # the file gitleaks opens as ``.gitleaks.toml``.
            if posixpath.normpath(path.replace("\\", "/")).lower() in _SELF_CONFIG_FILES:
                # Not a substring of the name: gitleaks' default allowlist skips ``gitleaks.toml``.
                alias = f"{_SCANNED_PREFIX}{len(renamed)}.txt"
                # A PR file may already use this name: overwriting it would drop it from the scan.
                while alias in content or alias in renamed:
                    alias = f"{_SCANNED_PREFIX}x{alias}"
                # Gitleaks reports the normalized path; callers map that back to the original.
                renamed[alias] = validate_relative_path(path)
                content[alias] = content.pop(path)
    directory = await materialize_into_temp_directory(content)
    try:
        report_path = directory / _REPORT_FILENAME
        await run_analyzer_subprocess(
            _argv(report_path), cwd=directory, timeout=timeout, env=environ
        )
        # Off the event loop, same as materialize_into_temp_directory/
        # cleanup_temp_directory above — _read_report does synchronous
        # Path.stat()/read_text() and would otherwise block the loop for
        # its duration on every gitleaks invocation (including every
        # per-finding redaction call in GitleaksTextRedactor.redact).
        entries = await asyncio.to_thread(_read_report, report_path)
        for entry in entries:
            if entry.get("File") in renamed:
                entry["File"] = renamed[entry["File"]]
        return entries
    finally:
        await cleanup_temp_directory(directory)


class GitleaksAnalyzer(ChildEnvironmentMixin):
    """``review.static_analysis.StaticAnalyzer`` implementation backed by
    the vendored Gitleaks binary. See this module's own docstring for the
    full design.
    """

    name = "gitleaks"

    def __init__(
        self, *, ignore_pr_config: bool = False, environ: Mapping[str, str] | None = None
    ) -> None:
        self._ignore_pr_config = ignore_pr_config
        super().__init__(environ=environ)

    async def run(
        self,
        diff: Diff,
        content: dict[str, bytes],
        *,
        timeout: float,
        config_files: Mapping[str, bytes],
    ) -> list[Finding]:
        # Gitleaks reports the materialized (validate_relative_path-
        # normalized) path it scanned, which only differs from `content`'s
        # own key when a path contains a backslash.
        original_by_normalized = original_path_mapping(content)
        entries = await _run_gitleaks(
            content, timeout=timeout, ignore_pr_config=self._ignore_pr_config, environ=self._environ
        )
        return [_to_finding(entry, original_by_normalized) for entry in entries]


class GitleaksTextRedactor(ChildEnvironmentMixin):
    """``findings.redact.TextRedactor`` implementation backed by the same
    vendored Gitleaks binary. See this module's own docstring for the full
    design and ``_redact_matched_lines``'s own docstring for why redaction
    is whole-line, not column-precise.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        ignore_pr_config: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._ignore_pr_config = ignore_pr_config
        super().__init__(environ=environ)

    async def redact(self, text: str) -> str:
        entries = await _run_gitleaks(
            # Finding text can quote attacker-controlled source; with `ignore_pr_config`
            # `_run_gitleaks` defuses any inline allow marker.
            {_REDACTION_SCRATCH_FILENAME: text.encode("utf-8")},
            timeout=self._timeout_seconds,
            ignore_pr_config=self._ignore_pr_config,
            environ=self._environ,
        )
        if not entries:
            return text
        return _redact_matched_lines(text, entries)


# Runtime confirmation (not just a docstring claim) that these classes
# actually satisfy the Protocols they're built to implement.
_: type[StaticAnalyzer] = GitleaksAnalyzer
_2: type[TextRedactor] = GitleaksTextRedactor


class GitleaksSecretLineFinder(ChildEnvironmentMixin):
    """One batched Gitleaks invocation over many texts, answering which lines of each hold a secret.

    Used by local mode's input-side redaction (``adapter/local/redaction.py`` defines the Protocol
    this satisfies). Every text becomes one scratch file in a single mode-0700 temp directory
    (``mkdtemp``) that ``_run_gitleaks`` removes in its own ``finally``, so a diff of any size costs
    one subprocess, never one per file or hunk. Caps on file count and bytes are the caller's job
    and are enforced before this runs; ``timeout_seconds`` is the overall deadline for the call
    (materialize, scan and read the report). Any failure raises: the caller fails closed.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float,
        ignore_pr_config: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._ignore_pr_config = ignore_pr_config
        super().__init__(environ=environ)

    async def secret_lines(self, texts: Sequence[str]) -> list[frozenset[int]]:
        """Per text, the 1-based line numbers (multi-line matches included) Gitleaks flagged."""
        if not texts:
            return []
        names = {f"t{index}.txt": index for index in range(len(texts))}
        # With `ignore_pr_config`, `_run_gitleaks` defuses inline allow markers.
        content = {name: texts[index].encode("utf-8") for name, index in names.items()}
        async with asyncio.timeout(self._timeout_seconds):
            entries = await _run_gitleaks(
                content,
                timeout=self._timeout_seconds,
                ignore_pr_config=self._ignore_pr_config,
                environ=self._environ,
            )
        lines: list[set[int]] = [set() for _ in texts]
        for entry in entries:
            index = names.get(entry.get("File", ""))
            start = entry.get("StartLine")
            end = entry.get("EndLine", start)
            # An entry that cannot be placed would be a secret left in: fail rather than skip it.
            if index is None or not isinstance(start, int) or not isinstance(end, int):
                raise AnalyzerOutputParseError(
                    tool="gitleaks", detail="a report entry could not be matched to its input"
                )
            lines[index].update(range(start, end + 1))
        return [frozenset(found) for found in lines]
