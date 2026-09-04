"""Typed exceptions for adapter.analyzers.

Per docs/rules/architecture.md's "specific exception types per domain
concern" rule — mirrors adapter/github/exceptions.py's and
adapter/storage/exceptions.py's own pattern.
"""

from __future__ import annotations


class AnalyzerAdapterError(Exception):
    """Base class for every error this adapter raises."""


class PathTraversalError(AnalyzerAdapterError):
    """Raised by ``adapter.analyzers.subprocess_runner`` when a
    PR-controlled filename would escape the per-invocation materialization
    directory — a ``..`` path segment, an absolute path, or a path that
    resolves outside that directory once normalized.

    Per the plan's Phase 4 section: every PR-controlled filename is
    validated before it is joined onto the temp-directory path, since a
    PR's file list is the first PR-controlled data this platform writes to
    the Worker's local filesystem.
    """

    def __init__(self, *, path: str, detail: str) -> None:
        super().__init__(f"path traversal rejected for {path!r}: {detail}")
        self.path = path
        self.detail = detail


# ---------------------------------------------------------------------------
# Phase 5: shared subprocess-execution helper (adapter.analyzers.subprocess_runner)
# ---------------------------------------------------------------------------


class MaterializationCollisionError(AnalyzerAdapterError):
    """Raised by ``adapter.analyzers.subprocess_runner.materialize_files``
    when two distinct content keys normalize to the same on-disk relative
    path (e.g. a backslash- and forward-slash-separated variant of the same
    path) — the second ``os.open(..., O_EXCL)`` call would otherwise raise a
    bare, untyped ``FileExistsError`` instead of one of this module's own
    typed errors."""

    def __init__(self, *, path: str) -> None:
        super().__init__(f"materialization collision: {path!r} was already written")
        self.path = path


class AnalyzerInvocationError(AnalyzerAdapterError):
    """Raised by ``adapter.analyzers.subprocess_runner.run_analyzer_subprocess``
    when the analyzer process exits with a non-zero status — a genuine tool
    failure (bad invocation, a crash/panic), not "the tool found something"
    (Gitleaks' and Semgrep's own invocations are both built to treat
    "findings present" as a zero exit, per their own module docstrings, so
    a non-zero exit here is never that case)."""

    def __init__(self, *, argv: list[str], returncode: int, output: bytes) -> None:
        # Only the tail of `output` is embedded in the message (the whole
        # captured stream can be up to `max_output_bytes` — no need to
        # duplicate all of it into an exception message an operator reads
        # in a log line already carrying the full output separately).
        tail = output[-2000:].decode("utf-8", errors="replace")
        super().__init__(f"analyzer invocation {argv!r} exited {returncode}; output tail: {tail!r}")
        self.argv = argv
        self.returncode = returncode
        self.output = output


class AnalyzerTimeoutError(AnalyzerAdapterError, TimeoutError):
    """Raised when an analyzer subprocess is killed for exceeding its own
    configured timeout, per the plan's Phase 5 "Subprocess lifecycle on
    timeout/cancellation" section.

    Inherits from the builtin ``TimeoutError`` (not just
    ``AnalyzerAdapterError``) so ``review.pipeline.StepRegistry``'s own
    ``isinstance(exc, TimeoutError)`` status classification
    (``"timeout"`` vs. ``"failed"``) still holds regardless of whether the
    outer registry's own ``asyncio.wait_for`` or this module's own internal
    one is what actually fires first for a given call.
    """

    def __init__(self, *, argv: list[str], timeout_seconds: float) -> None:
        super().__init__(f"analyzer invocation {argv!r} killed after {timeout_seconds}s")
        self.argv = argv
        self.timeout_seconds = timeout_seconds


class AnalyzerOutputTooLargeError(AnalyzerAdapterError):
    """Raised when an analyzer subprocess's combined stdout/stderr exceeds
    ``max_output_bytes`` before the process finished — the process is
    killed and reaped rather than left running or read further, per the
    plan's Phase 5 "Bounded memory" section."""

    def __init__(self, *, argv: list[str], max_output_bytes: int) -> None:
        super().__init__(
            f"analyzer invocation {argv!r} exceeded the {max_output_bytes}-byte output cap"
        )
        self.argv = argv
        self.max_output_bytes = max_output_bytes


class AnalyzerOutputParseError(AnalyzerAdapterError):
    """Raised by a concrete analyzer adapter (``adapter/analyzers/gitleaks.py``,
    ``adapter/analyzers/semgrep.py``) when a subprocess that exited zero
    nonetheless produced output this platform can't parse into
    ``findings.models.Finding`` objects — a plausible real-world case
    (tool-version drift against the pinned ruleset, truncated output), per
    the plan's Phase 5 Tests section, treated as a typed, isolated sub-step
    failure rather than an unhandled parse exception."""

    def __init__(self, *, tool: str, detail: str) -> None:
        super().__init__(f"{tool}: could not parse analyzer output: {detail}")
        self.tool = tool
        self.detail = detail
