"""adapter.analyzers.subprocess_runner — safe materialization of
already-fetched file content onto the local filesystem, per the plan's
Phase 4 "File materialization safety" section.

**Split of responsibilities, corrected during this plan's own review (see
the plan's Phase 4 section for the full history):** GitHub-API content
fetching stays in ``adapter/github/diff.py`` (the one package that owns
"how this platform talks to GitHub"); the mode-based symlink/submodule
guard lives there too, since that's where the fetch *decision* happens
(``adapter/github/diff.py``'s ``fetch_content`` never fetches a symlink/
submodule entry's blob at all). This module does only what an analyzer
adapter should: given already-fetched bytes (never a GitHub call of its
own), validate each PR-controlled filename is safe to write, then write it.

**Path-traversal guard (``validate_relative_path``) is pure, no-I/O
logic** — string/segment inspection only, never ``pathlib.Path.resolve()``
(which needs the path to already exist to normalize safely on every
platform) — tested at Unit tier despite living inside ``adapter/analyzers``,
per ``docs/adr/0005-pure-no-io-logic-unit-tested.md``. Every PR-controlled
filename is rejected outright if it contains a ``..`` path segment,
resolves outside the materialization directory once normalized, or is an
absolute path — logged at ``warning`` with the rejected filename before
``PathTraversalError`` is raised, since a path-traversal attempt is itself
worth an operator seeing, the same as the symlink/submodule case
``adapter/github/diff.py`` logs at ``info`` — a PR's file list is the first
PR-controlled data this platform writes to the Worker's local filesystem.
Logging is not filesystem/network/subprocess/database I/O, so this
warning-log call does not disqualify the guard from this module's own
"pure, no-I/O" classification above.

``materialize_files`` is the one place real filesystem I/O happens here:
writes each already-fetched, already-validated file via ``O_CREAT |
O_EXCL`` — a write only ever creates a new regular file at the target,
never follows an existing path there, never creates a symlink itself.

**Phase 5 extension — the actual analyzer-subprocess-spawning logic,** per
the plan's Phase 5 section (``adapter/analyzers/gitleaks.py``/``semgrep.py``
are this module's only callers):

- ``materialize_into_temp_directory``/``cleanup_temp_directory`` — a fresh,
  process-unique ``tempfile.mkdtemp`` directory per invocation (never a
  shared or predictable path), materialized via ``materialize_files``
  above and torn down in the caller's own ``finally`` block regardless of
  outcome. A teardown failure (e.g. a permissions error) is caught and
  logged at ``warning`` with the directory path rather than propagated — a
  leaked per-job temp directory should be visible in logs, not fail the
  whole invocation.
- ``run_analyzer_subprocess`` — spawns ``argv`` via
  ``asyncio.create_subprocess_exec`` (``stderr=asyncio.subprocess.STDOUT``,
  merging the child's stderr into the same already-capped, already-drained
  stream as its stdout — a chatty analyzer writing enough to fill an
  undrained OS pipe buffer would otherwise deadlock the call until the
  outer step timeout fires, and an inherited/unpiped stderr would let
  attacker-influenced tool output flow unbounded and unredacted straight
  into Worker container logs). Reads ``process.stdout`` in fixed-size
  chunks (never ``communicate()``, which accumulates unboundedly regardless
  of any ``limit`` kwarg), capping the running total at ``max_output_bytes``
  (default 10MB — orders of magnitude larger than either tool's expected
  JSON output for a review-sized diff, existing to catch pathological/
  adversarial output, not constrain ordinary tool behavior). Wrapped in its
  own ``asyncio.wait_for(..., timeout=timeout)`` — every caller (an analyzer
  sub-step, or ``findings.redact.TextRedactor``'s own short-lived redaction
  calls) passes its own timeout in explicitly, reusing this one mechanism
  rather than each caller wrapping its own call in a second ``wait_for`` of
  its own. **Subprocess lifecycle on timeout/cancellation, the property this
  function exists to guarantee:** neither this function's own internal
  timeout firing nor an *outer* cancellation (e.g. the sub-step registry's
  own ``asyncio.wait_for`` cancelling this coroutine from the outside) stops
  the underlying OS process by itself — cancelling the coroutine awaiting
  ``process.stdout.read()`` only stops the Python-side await. Both paths are
  caught here (``TimeoutError`` from this function's own internal
  ``wait_for``; ``asyncio.CancelledError`` from an outer one) and both call
  ``process.kill()`` then ``await process.wait()`` to reap the now-dead
  child before propagating — the single kill-and-reap mechanism every
  subprocess spawn site in this plan shares (both analyzer sub-steps, and
  every ``TextRedactor`` invocation), not two divergent ones. A non-zero
  exit (the process ran to completion but failed) is a distinct case,
  raised as ``AnalyzerInvocationError`` rather than killed — the process
  already exited on its own.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from adapter.analyzers.exceptions import (
    AnalyzerInvocationError,
    AnalyzerOutputTooLargeError,
    AnalyzerTimeoutError,
    MaterializationCollisionError,
    PathTraversalError,
)

logger = logging.getLogger(__name__)

_MATERIALIZED_FILE_MODE = 0o644
_DEFAULT_MAX_OUTPUT_BYTES = 10_000_000  # ~10MB — see module docstring.
_READ_CHUNK_SIZE = 65536


def _reject(path: str, detail: str) -> PathTraversalError:
    """Log the rejected filename at ``warning`` and return the error to
    raise — a path-traversal attempt is itself worth an operator seeing,
    per the plan's Phase 4 "File materialization safety" section, the same
    as the symlink/submodule case ``adapter/github/diff.py`` logs at
    ``info``. Logging is not filesystem/network/subprocess/database I/O, so
    this keeps ``validate_relative_path`` pure per
    ``docs/adr/0005-pure-no-io-logic-unit-tested.md``.
    """
    logger.warning(
        "materialization rejected: path traversal attempt",
        extra={"path": path, "detail": detail},
    )
    return PathTraversalError(path=path, detail=detail)


def validate_relative_path(path: str) -> str:
    """Validate ``path`` (a PR-controlled filename) is safe to join onto a
    materialization directory, returning it unchanged (normalized to
    forward slashes) if so.

    Raises ``PathTraversalError`` if ``path`` is empty, absolute, contains
    a literal ``..`` path segment, or would resolve outside the
    materialization directory once normalized — three independent checks
    per the plan's own wording, not only the first that happens to catch a
    given malicious input. Every rejection is logged at ``warning`` with
    the rejected filename before the error is raised.
    """
    if not path:
        raise _reject(path, "empty path")

    normalized = path.replace("\\", "/")
    if posixpath.isabs(normalized):
        raise _reject(path, "absolute path")
    if ".." in normalized.split("/"):
        raise _reject(path, "contains a '..' path segment")

    collapsed = posixpath.normpath(normalized)
    if collapsed == ".." or collapsed.startswith("../") or posixpath.isabs(collapsed):
        raise _reject(path, "resolves outside the materialization directory")
    if collapsed in (".", ""):
        raise _reject(path, "empty/self-referential path")

    return normalized


def original_path_mapping(content: dict[str, bytes]) -> dict[str, str]:
    """Map each ``validate_relative_path``-normalized path back to its
    original ``content`` key, for analyzers (Gitleaks, Semgrep) that report
    findings against the normalized path they actually scanned on disk.

    Shared by both ``adapter/analyzers/gitleaks.py`` and
    ``adapter/analyzers/semgrep.py``'s ``run`` methods — a posted review
    comment must land on a path GitHub's PR review API recognizes (the
    original, un-normalized path), which only differs from the normalized
    one when a path contains a backslash. One definition so both analyzers'
    path-mapping behavior can't drift from each other.
    """
    return {validate_relative_path(path): path for path in content}


def materialize_files(content: dict[str, bytes], directory: Path) -> list[Path]:
    """Write ``content`` (already-fetched file bytes, keyed by their
    PR-controlled relative path — e.g. ``adapter/github/diff.py``'s
    ``fetch_content`` return value) into ``directory``, validating each
    path via ``validate_relative_path`` first.

    Raises ``PathTraversalError`` (propagated from the validation step,
    which has already logged the rejection at ``warning``) without writing
    anything for the offending entry — a caller (Phase 5's analyzer
    sub-step) decides whether one bad path should fail the whole
    materialization or be caught and skipped so the rest proceed; this
    function itself makes no such policy decision, per this module's own
    narrow responsibility.

    Raises ``MaterializationCollisionError`` if two distinct keys in
    ``content`` normalize (via ``validate_relative_path``) to the same
    on-disk relative path — without it, the second ``os.open(...,
    O_EXCL)`` would raise a bare, untyped ``FileExistsError`` instead of one
    of this module's own typed errors.
    """
    written: list[Path] = []
    for path, data in content.items():
        relative = validate_relative_path(path)
        target = directory / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except (FileExistsError, NotADirectoryError) as exc:
            # A previously-materialized entry sits at (or above) this
            # path's own parent and isn't itself a directory — e.g. content
            # already contains a real two-segment path "a/b" as a regular
            # file, and this path normalizes (via `validate_relative_path`)
            # to "a/b/c". `Path.mkdir(parents=True, exist_ok=True)` only
            # swallows `FileExistsError` when the existing entry actually
            # is a directory (confirmed against the real stdlib behavior,
            # not assumed) — otherwise it re-raises the bare
            # `FileExistsError` for the innermost colliding segment, or
            # (single-segment case) `NotADirectoryError` if a still-deeper
            # segment is required under it. Same underlying collision
            # `MaterializationCollisionError` below exists to report, just
            # discovered one level up.
            raise MaterializationCollisionError(path=path) from exc
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _MATERIALIZED_FILE_MODE)
        except (FileExistsError, IsADirectoryError) as exc:
            # `IsADirectoryError`: the reverse ordering of the same
            # collision — a previously-materialized nested path (e.g.
            # "a/b/c") already created "a/b" as a directory before this
            # entry ("a/b") was materialized as a file.
            raise MaterializationCollisionError(path=path) from exc
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        written.append(target)
    return written


def _materialize_into_temp_directory_sync(content: dict[str, bytes]) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="review-analyzer-"))
    try:
        materialize_files(content, directory)
    except BaseException:
        _cleanup_temp_directory_sync(directory)
        raise
    return directory


async def materialize_into_temp_directory(content: dict[str, bytes]) -> Path:
    """Creates a fresh, process-unique temp directory
    (``tempfile.mkdtemp`` — never a shared or predictable path) and
    materializes ``content`` into it via ``materialize_files`` above. The
    one place a Gitleaks/Semgrep invocation's own scratch directory is
    created; the caller is responsible for tearing it down (via
    ``cleanup_temp_directory``, in its own ``finally`` block) regardless of
    the invocation's outcome.

    If ``materialize_files`` raises (e.g. ``PathTraversalError`` on a
    PR-controlled filename) *after* the directory already exists on disk,
    this function tears the now-partially-written directory down itself
    before propagating — the caller's own ``finally`` block only runs once
    this function has already returned, so without this the freshly created
    directory would otherwise leak on every such failure.

    Runs on a worker thread (``asyncio.to_thread``) — ``mkdtemp``/``os.open``/
    ``os.write`` are blocking filesystem calls; per docs/rules/architecture.md's
    "I/O is async" rule, an async caller must not block the event loop on
    them directly, especially since this runs once per analyzer invocation
    (potentially many times concurrently, including the redaction fan-out
    in ``findings/redact.py``).
    """
    return await asyncio.to_thread(_materialize_into_temp_directory_sync, content)


def _cleanup_temp_directory_sync(directory: Path) -> None:
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        logger.warning(
            "temp directory teardown failed",
            extra={"directory": str(directory), "error": str(exc)},
        )


async def cleanup_temp_directory(directory: Path) -> None:
    """Tears down a directory created by ``materialize_into_temp_directory``.

    A teardown failure (e.g. a permissions error) is caught and logged at
    ``warning`` with the directory path rather than propagated — per the
    plan's Phase 5 Observability section, a leaked per-job temp directory
    should be visible in logs, not fail the whole invocation or mask
    whatever the invocation's own outcome was.

    Runs on a worker thread (``asyncio.to_thread``) — see
    ``materialize_into_temp_directory``'s own docstring for why
    ``shutil.rmtree`` must not block the event loop directly.
    """
    await asyncio.to_thread(_cleanup_temp_directory_sync, directory)


class _OutputCapExceeded(Exception):
    """Internal signal only — never escapes this module. Raised by
    ``_read_capped`` when the running total exceeds ``max_output_bytes``;
    ``run_analyzer_subprocess`` catches it and raises the typed, public
    ``AnalyzerOutputTooLargeError`` instead.
    """


async def _read_capped(process: asyncio.subprocess.Process, max_output_bytes: int) -> bytes:
    """Reads ``process.stdout`` (stderr already merged into it, per
    ``run_analyzer_subprocess``'s own ``stderr=STDOUT``) in fixed-size
    chunks, accumulating into a buffer while checking the running byte
    total against ``max_output_bytes`` — never ``communicate()``, which
    accumulates unboundedly regardless of any ``limit`` kwarg (that kwarg
    only bounds ``StreamReader`` line-based backpressure, not
    ``communicate()``'s total accumulated size). Raises
    ``_OutputCapExceeded`` (internal only) the moment the cap is exceeded,
    without waiting for the process to finish.
    """
    assert process.stdout is not None  # guaranteed by stdout=PIPE at spawn time
    buffer = bytearray()
    while True:
        chunk = await process.stdout.read(_READ_CHUNK_SIZE)
        if not chunk:
            return bytes(buffer)
        buffer.extend(chunk)
        if len(buffer) > max_output_bytes:
            raise _OutputCapExceeded()


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    """The one kill-and-reap mechanism every subprocess spawn site in this
    plan shares (see the module docstring's "Subprocess lifecycle" note).
    ``process.kill()`` alone does not reap the child — without the
    following ``await process.wait()``, the now-dead process stays a
    zombie until something else reaps it.
    """
    if process.returncode is None:
        process.kill()
    await process.wait()


_CREDENTIAL_ENV_SUFFIX = "_PRODUCT_KNOWLEDGE_CREDENTIALS"


def analyzer_env(environ: Mapping[str, str]) -> dict[str, str]:
    """
    Return the child environment: ``environ`` minus the product-knowledge credential map.

    The credential map is ``REVIEW_<ENTRYPOINT>_PRODUCT_KNOWLEDGE_CREDENTIALS``.
    Analyzers run over customer-controlled repo content (semgrep rules,
    custom configs) and must never inherit provider bearer tokens. Every
    other variable is kept so the tools keep finding PATH, HOME, etc.
    ponytail: deny-list of one name; an allow-list env if more secrets land
    in the worker environment.
    """
    return {k: v for k, v in environ.items() if not k.endswith(_CREDENTIAL_ENV_SUFFIX)}


async def run_analyzer_subprocess(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> bytes:
    """Runs ``argv`` (an analyzer tool invocation, or a
    ``findings.redact.TextRedactor`` redaction call) via
    ``asyncio.create_subprocess_exec``, bounded by both ``timeout`` and
    ``max_output_bytes`` — see the module docstring for the full
    kill-and-reap/bounded-read design. Returns the captured, merged
    stdout+stderr bytes on a zero exit; raises ``AnalyzerInvocationError``
    (non-zero exit), ``AnalyzerTimeoutError`` (killed for exceeding
    ``timeout``), or ``AnalyzerOutputTooLargeError`` (killed for exceeding
    ``max_output_bytes``) otherwise. A genuine outer cancellation (e.g. the
    caller's own coroutine being cancelled by something *other* than this
    function's own ``timeout``) is not swallowed — the process is still
    killed and reaped, then ``asyncio.CancelledError`` is re-raised
    unchanged, per this codebase's own "never swallow or log-and-continue
    silently" rule.
    """
    argv_list = list(argv)
    process = await asyncio.create_subprocess_exec(
        *argv_list,
        cwd=cwd,
        env=analyzer_env(os.environ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    read_started = time.monotonic()
    try:
        output = await asyncio.wait_for(_read_capped(process, max_output_bytes), timeout=timeout)
    except TimeoutError as exc:
        await _kill_and_reap(process)
        logger.error(
            "analyzer step killed after exceeding its timeout",
            extra={"argv": argv_list, "timeout_seconds": timeout},
        )
        raise AnalyzerTimeoutError(argv=argv_list, timeout_seconds=timeout) from exc
    except asyncio.CancelledError:
        await _kill_and_reap(process)
        raise
    except _OutputCapExceeded as exc:
        await _kill_and_reap(process)
        logger.error(
            "analyzer output exceeded the size cap",
            extra={"argv": argv_list, "max_output_bytes": max_output_bytes},
        )
        raise AnalyzerOutputTooLargeError(
            argv=argv_list, max_output_bytes=max_output_bytes
        ) from exc

    try:
        # ``_read_capped`` returning just means the pipe's write end
        # closed (EOF) — not necessarily that the process has actually
        # exited (e.g. it closes stdout during its own cleanup). Bounded by
        # the *remaining* budget (not a fresh ``timeout``), so a process
        # that closes stdout quickly but then hangs is still killed within
        # this function's own overall ``timeout`` window rather than up to
        # ~2x it.
        # If the process has already been reaped (returncode populated),
        # ``process.wait()`` returns immediately regardless of ``timeout`` —
        # but skip the ``wait_for`` call entirely in that case anyway, since
        # a ``remaining`` budget that rounds to ~0 could otherwise cancel the
        # wait before the event loop gets a turn to observe an
        # already-exited process, spuriously killing a run that in fact
        # already finished successfully.
        if process.returncode is None and time.monotonic() - read_started >= timeout:
            # ``remaining`` is about to round to ~0. The process may have
            # already exited but not yet been reaped by the event loop's
            # child watcher (which only runs on its own turn) — yield once
            # so a same-tick exit gets the chance to populate ``returncode``
            # before we commit to a ``wait_for`` that would otherwise cancel
            # immediately (per ``asyncio.wait_for``'s ``timeout <= 0``
            # short-circuit) and spuriously kill an already-finished run.
            await asyncio.sleep(0)
        if process.returncode is not None:
            returncode = process.returncode
        else:
            remaining = max(0.0, timeout - (time.monotonic() - read_started))
            returncode = await asyncio.wait_for(process.wait(), timeout=remaining)
    except TimeoutError as exc:
        await _kill_and_reap(process)
        logger.error(
            "analyzer step killed after exceeding its timeout",
            extra={"argv": argv_list, "timeout_seconds": timeout},
        )
        raise AnalyzerTimeoutError(argv=argv_list, timeout_seconds=timeout) from exc
    except asyncio.CancelledError:
        await _kill_and_reap(process)
        raise

    if returncode != 0:
        raise AnalyzerInvocationError(argv=argv_list, returncode=returncode, output=output)
    return output
