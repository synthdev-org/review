"""review.context_symbols — diff-to-chunk resolution (Unit A of the Context
Builder), per docs/plans/plan-deliver-context-builder.md's Phase 1 section
and docs/specs/spec-context-builder.md's US1/US10.

Maps each changed diff hunk to the enclosing function/method/class/
interface symbol it falls inside, on both sides of the change — the
pre-change side (covering modified, renamed, and removed symbols uniformly
under their *pre-change* identity, US10) feeds Phase 2's blast-radius graph
query; the post-change side feeds Phase 3's similarity-query embedding
call.

**``resolve_changed_symbols`` is pure, no-I/O matching logic** — it is
handed already-fetched ``pre_content``/``post_content`` dicts and performs
no I/O of its own beyond CPU-bound Tree-sitter parsing (dispatched via
``asyncio.to_thread``, never blocking I/O) — per
[ADR-0005](../docs/adr/0005-pure-no-io-logic-unit-tested.md), whose own
"Scope, extended per phase" list this phase's own definition of done
requires updating to name this case (done in that ADR's own text).
``fetch_diff_content`` below, by contrast, *is* real I/O (two Protocol
calls) and is proven only by Phase 5's end-to-end integration test, not a
unit test of its own — see this phase's own Tests subsection.

Zero infra imports, per architecture.md's domain-package rule.
``indexing.chunk``/``indexing.tree`` are both domain packages too — this
module importing their types directly is a deliberate, named
cross-bounded-context dependency (Assumption 4), the same ordinary shape
``review/review_generation.py`` already has on ``llmgateway.models``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from indexing.chunk import Chunk, ChunkParseError, chunk_file, language_for_path
from indexing.tree import RepoTreeFetcher
from ingestion.diff import Diff, DiffFetcher

logger = logging.getLogger(__name__)

# Assumption 6's upstream ceiling on how many items generate a query at
# all — applied to this module's own output, *before* Phase 2/3 ever issue
# a query. Hardcoded module-level constants, not `WorkerSettings` fields
# (round four resolved this ambiguity): spec-context-builder.md's own
# Priority section already decided a fixed default is sufficient, mirroring
# `indexing/pipeline.py`'s own `FULL_RUN_FILE_CEILING`-style constants
# rather than a new operator-facing setting.
MAX_CHANGED_SYMBOLS_PER_JOB = 50
MAX_CHANGED_CHUNKS_PER_JOB = 50

# Matches a unified-diff hunk header, e.g. `@@ -12,7 +12,9 @@ optional text`.
# `b`/`d` (the line counts) are each optional in the unified-diff format
# itself — a single-line hunk omits them, implying a count of 1.
_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))?"
    r" \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass(frozen=True)
class _HunkRange:
    """One parsed ``@@ -a,b +c,d @@`` hunk header's pre-/post-change,
    1-indexed, inclusive-both-ends line ranges.

    A count of ``0`` means this side of the hunk touches no lines at all —
    unified diff's own convention for a pure addition (``old_count == 0``,
    nothing removed) or a pure deletion (``new_count == 0``, nothing
    added) — and is treated as an empty range contributing no resolved
    symbol on that side, never an error.
    """

    pre_start_line: int
    pre_line_count: int
    post_start_line: int
    post_line_count: int


def _parse_hunk_headers(patch: str) -> tuple[_HunkRange, ...]:
    """Parses every ``@@ -a,b +c,d @@`` hunk header out of a unified-diff
    patch's text. Confirmed no existing hunk-header parser exists elsewhere
    in this codebase to reuse (per the plan's Phase 1 section) — this is
    genuinely new parsing logic, not a wrapper around one.
    """

    hunks: list[_HunkRange] = []
    for line in patch.splitlines():
        match = _HUNK_HEADER_RE.match(line)
        if match is None:
            continue
        hunks.append(
            _HunkRange(
                pre_start_line=int(match.group("old_start")),
                pre_line_count=int(match.group("old_count") or "1"),
                post_start_line=int(match.group("new_start")),
                post_line_count=int(match.group("new_count") or "1"),
            )
        )
    return tuple(hunks)


@dataclass(frozen=True)
class _LineByteIndex:
    """Precomputed per-line byte-prefix sums for one file's content, so a
    hunk's ``(start_line, line_count)`` can be converted to a byte range in
    O(1) instead of re-splitting and re-summing the whole file from scratch.

    ``resolve_changed_symbols`` builds one of these per file per side
    *once*, before its hunk loop, and reuses it for every hunk touching
    that file — a file with many hunks previously paid the full
    split-and-sum cost once per hunk (O(hunks * file_size)); building this
    index once makes the total cost O(file_size + hunks).
    """

    line_count: int
    # `prefix_bytes[i]` is the total byte length of the first `i` lines
    # (0-indexed prefix sum); `prefix_bytes[0] == 0`.
    prefix_bytes: tuple[int, ...]

    @classmethod
    def build(cls, content: bytes) -> _LineByteIndex:
        # Split on `\n` only, never `bytes.splitlines()` (which also treats
        # a bare `\r` as its own line break) -- a unified diff's hunk
        # header line count is itself derived from `\n`-delimited lines
        # (the patch format's own line separator, regardless of the file's
        # line-ending convention), the same convention
        # `indexing/chunk.py`'s `_line_range` newline-counts against. A
        # file with old Mac-style bare-`\r` line endings would otherwise
        # make this split more "lines" than the hunk header (and
        # `_line_range`) agree exist, misaligning the computed byte range
        # against `Chunk.byte_range` for overlap resolution.
        parts = content.split(b"\n")
        # `parts[:-1]` are always real, `\n`-terminated lines; `parts[-1]`
        # is a real, un-terminated final line only when content doesn't
        # itself end in `\n` -- when it does, `parts[-1]` is an empty
        # phantom entry `split` contributes after the final separator, and
        # must NOT be counted as an extra line (matching
        # `bytes.splitlines(keepends=True)`'s own behavior for the same
        # content, e.g. `b"a\nb\n"` has 2 lines, not 3).
        lines = [part + b"\n" for part in parts[:-1]]
        if parts[-1]:
            lines.append(parts[-1])
        prefix = [0]
        total = 0
        for line in lines:
            total += len(line)
            prefix.append(total)
        return cls(line_count=len(lines), prefix_bytes=tuple(prefix))

    def line_range_to_byte_range(self, start_line: int, line_count: int) -> tuple[int, int] | None:
        """Converts a 1-indexed ``(start_line, line_count)`` hunk-side
        range into a file-absolute byte range using this codebase's
        existing ``[start, end)`` exclusive-end convention (the same one
        ``indexing.chunk.Chunk.byte_range`` uses) — so it can be compared
        directly against a parsed ``Chunk``'s own ``byte_range`` for
        overlap.

        Returns ``None`` for an empty range (``line_count == 0`` — a pure
        addition/deletion hunk's untouched side) or one that starts beyond
        this index's own line count (a hunk header referencing a line
        range that no longer makes sense against the content actually
        fetched — treated the same as "no resolved symbol", never an
        error).
        """

        if line_count <= 0:
            return None
        if start_line < 1 or start_line > self.line_count:
            return None
        end_line = min(start_line + line_count - 1, self.line_count)
        return self.prefix_bytes[start_line - 1], self.prefix_bytes[end_line]


def _line_range_to_byte_range(
    content: bytes, start_line: int, line_count: int
) -> tuple[int, int] | None:
    """Single-call convenience wrapper around ``_LineByteIndex`` — builds
    the index from ``content`` and converts one range in one call. Kept for
    callers (and tests) that only need to resolve a single range; a caller
    resolving many ranges against the same ``content`` (e.g.
    ``resolve_changed_symbols`` resolving every hunk in a file) should
    build one ``_LineByteIndex`` directly and reuse it instead, to avoid
    re-splitting and re-summing the same content once per range.
    """

    return _LineByteIndex.build(content).line_range_to_byte_range(start_line, line_count)


def _ranges_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _resolve_single_enclosing_chunk(
    chunks: tuple[Chunk, ...], hunk_byte_range: tuple[int, int] | None
) -> Chunk | None:
    """Returns the one ``Chunk`` whose ``byte_range`` overlaps
    ``hunk_byte_range``, or ``None`` when zero or more than one chunk
    overlaps — per US1, a hunk resolves to a symbol only when it falls
    inside exactly one enclosing declaration; an import-only change, a
    top-level constant, or a hunk spanning more than one declaration all
    land here as "no resolved symbol", not an error.
    """

    if hunk_byte_range is None:
        return None
    overlapping = [chunk for chunk in chunks if _ranges_overlap(chunk.byte_range, hunk_byte_range)]
    if len(overlapping) != 1:
        return None
    return overlapping[0]


class ChangedSymbols(BaseModel):
    """``resolve_changed_symbols``'s return value: the ``Chunk``s a PR's
    diff hunks resolved to, on each side of the change.

    ``pre_change_hits``' own ``symbol_name`` is what Phase 2's blast-radius
    query looks up — covering modified, renamed, and removed symbols
    uniformly under their pre-change identity (US10), since a hunk is
    always resolved against the *pre-change* file content for this side
    regardless of whether the same name still exists post-change.
    ``post_change_hits``' own ``content`` is what Phase 3 embeds for the
    similarity query. Both are already deduplicated (a chunk touched by
    more than one hunk contributes once, in first-touched order) and capped
    at ``MAX_CHANGED_SYMBOLS_PER_JOB``/``MAX_CHANGED_CHUNKS_PER_JOB``
    respectively, dropped deterministically in diff order.

    ``pre_dropped_count``/``post_dropped_count`` carry forward how many
    items each of those two upstream ceilings actually dropped (``0`` when
    under ceiling) — Phase 5's own Observability requirement is that
    ``ContextBuilderStep``'s per-run log surfaces "dropped-by-ceiling"
    counts for *both* this module's upstream, per-job ceilings and
    ``review.context_bundle``'s existing per-item ceilings, not just the
    immediate ``warning`` logged here at drop time.
    """

    model_config = ConfigDict(frozen=True)

    pre_change_hits: tuple[Chunk, ...]
    post_change_hits: tuple[Chunk, ...]
    pre_dropped_count: int = 0
    post_dropped_count: int = 0


def _apply_ceiling(items: list[Chunk], ceiling: int) -> tuple[list[Chunk], int]:
    """Keeps the first ``ceiling`` items, returning ``(kept, dropped_count)``
    — the same "leading survivors, trailing excess dropped" shape
    ``indexing/pipeline.py``'s own ``_apply_cost_ceilings`` and
    ``review/context_bundle.py``'s own ``_apply_per_item_cap`` each use —
    a small, deliberate per-module duplication (this module has no import
    of ``review.context_bundle``, and shouldn't gain one for a three-line
    helper: Phase 4 depends on Phases 1-3's *output shapes*, not their
    code, per this plan's own Phase ordering rationale, and reaching for a
    Phase 4 module's own internal, underscore-prefixed helper from here
    would invert that direction for no real benefit)."""

    if len(items) <= ceiling:
        return items, 0
    return items[:ceiling], len(items) - ceiling


def _resolve_one_side(
    *,
    index: _LineByteIndex,
    chunks: tuple[Chunk, ...],
    start_line: int,
    line_count: int,
    side: str,
    file_path: str,
    job_id: str,
    seen: set[tuple[str, int]],
    hits: list[Chunk],
) -> None:
    """Resolves one hunk's one side (``"pre-change"``/``"post-change"``) to
    its enclosing chunk, appending to ``hits``/``seen`` in place — shared by
    both sides of ``resolve_changed_symbols``'s hunk loop, which previously
    duplicated this exact byte-range-lookup / mismatch-warning /
    dedup-and-append sequence once per side.
    """

    byte_range = index.line_range_to_byte_range(start_line, line_count)
    # `line_range_to_byte_range` returns `None` for two distinct reasons: a
    # legitimately empty hunk side (`line_count <= 0`, the normal
    # pure-add/pure-delete case) or a hunk header whose `start_line` falls
    # beyond the fetched content's own line count — the latter means the
    # diff's hunk metadata and the content actually fetched for this file
    # disagree (e.g. a race between the diff being computed and the blob
    # being fetched). Only `line_count > 0` can distinguish the two here,
    # so it's the guard for logging the mismatch instead of silently
    # treating it the same as "no resolved symbol".
    if byte_range is None and line_count > 0:
        logger.warning(
            "context symbols: %s hunk line range exceeds fetched content; diff/content mismatch",
            side,
            extra={
                "job_id": job_id,
                "file_path": file_path,
                "start_line": start_line,
                "line_count": line_count,
            },
        )
    hit = _resolve_single_enclosing_chunk(chunks, byte_range)
    if hit is not None:
        key = (file_path, hit.chunk_index)
        if key not in seen:
            seen.add(key)
            hits.append(hit)


async def resolve_changed_symbols(
    diff: Diff,
    pre_content: dict[str, bytes],
    post_content: dict[str, bytes],
    *,
    job_id: str,
) -> ChangedSymbols:
    """Resolves every changed file's diff hunks to their enclosing chunk, on
    both the pre-change and post-change side, per this module's own
    docstring and US1/US10.

    For each changed, in-scope-language file with a patch: parses its hunk
    headers, chunks ``pre_content[path]``/``post_content[path]`` (each via
    ``indexing.chunk.chunk_file``, dispatched through ``asyncio.to_thread``
    since Tree-sitter parsing is CPU-bound and must stay off the Worker's
    event loop — mirroring ``indexing/pipeline.py``'s own precedent for this
    exact call, and Phase 3's own embedding-call precedent), and finds each
    hunk's single overlapping chunk (if any) on each side.

    A file whose language isn't one of this platform's three supported
    languages contributes nothing (US8, "out of scope, not partially
    handled"). A file absent from ``pre_content`` (the normal case for a
    newly-added file, which has no base-tree content to speak of) simply
    contributes no pre-change hits for that file — logged at ``info``, not a
    warning. A ``ChunkParseError`` from chunking either side of one file is
    caught for that file alone, logged at ``warning``, and that whole file
    contributes no symbols on either side (US8's per-file isolation) — the
    rest of the diff's files are unaffected.

    ``job_id`` is threaded through purely so this function's own
    ``ChunkParseError``/ceiling-truncation ``warning`` logs can be
    correlated to the originating job, mirroring the identical fix already
    applied to Phase 4's ``assemble_context_bundle``.
    """

    pre_hits: list[Chunk] = []
    pre_seen: set[tuple[str, int]] = set()
    post_hits: list[Chunk] = []
    post_seen: set[tuple[str, int]] = set()

    for file in diff.files:
        if file.patch is None:
            # No patch to resolve hunks from at all (e.g. GitHub omitted it
            # for a binary file, or a diff too large to render) — nothing
            # to do for this file.
            continue

        language = language_for_path(file.path)
        if language is None:
            # Out-of-scope language (US8) — contributes nothing, not an
            # error.
            continue

        hunks = _parse_hunk_headers(file.patch)
        if not hunks:
            continue

        pre_chunks: tuple[Chunk, ...] = ()
        post_chunks: tuple[Chunk, ...] = ()
        pre_index: _LineByteIndex | None = None
        post_index: _LineByteIndex | None = None
        try:
            if file.path in pre_content:
                pre_result = await asyncio.to_thread(
                    chunk_file, language, file.path, pre_content[file.path]
                )
                pre_chunks = pre_result.chunks
                # Built once per file, before the hunk loop below, and
                # reused for every hunk touching this file — see
                # `_LineByteIndex`'s own docstring for why.
                pre_index = _LineByteIndex.build(pre_content[file.path])
            else:
                logger.info(
                    "context symbols: file absent from base tree (newly added); "
                    "no pre-change chunks",
                    extra={"job_id": job_id, "file_path": file.path},
                )

            if file.path in post_content:
                post_result = await asyncio.to_thread(
                    chunk_file, language, file.path, post_content[file.path]
                )
                post_chunks = post_result.chunks
                post_index = _LineByteIndex.build(post_content[file.path])
        except ChunkParseError as exc:
            logger.warning(
                "context symbols: chunk parse failed; file contributes no symbols",
                extra={"job_id": job_id, "file_path": file.path, "error": str(exc)},
            )
            continue

        for hunk in hunks:
            if pre_chunks and pre_index is not None:
                _resolve_one_side(
                    index=pre_index,
                    chunks=pre_chunks,
                    start_line=hunk.pre_start_line,
                    line_count=hunk.pre_line_count,
                    side="pre-change",
                    file_path=file.path,
                    job_id=job_id,
                    seen=pre_seen,
                    hits=pre_hits,
                )

            if post_chunks and post_index is not None:
                _resolve_one_side(
                    index=post_index,
                    chunks=post_chunks,
                    start_line=hunk.post_start_line,
                    line_count=hunk.post_line_count,
                    side="post-change",
                    file_path=file.path,
                    job_id=job_id,
                    seen=post_seen,
                    hits=post_hits,
                )

    capped_pre_hits, pre_dropped = _apply_ceiling(pre_hits, MAX_CHANGED_SYMBOLS_PER_JOB)
    if pre_dropped:
        logger.warning(
            "context symbols: max_changed_symbols_per_job ceiling exceeded; dropping excess",
            extra={
                "job_id": job_id,
                "ceiling": MAX_CHANGED_SYMBOLS_PER_JOB,
                "dropped_count": pre_dropped,
            },
        )

    capped_post_hits, post_dropped = _apply_ceiling(post_hits, MAX_CHANGED_CHUNKS_PER_JOB)
    if post_dropped:
        logger.warning(
            "context symbols: max_changed_chunks_per_job ceiling exceeded; dropping excess",
            extra={
                "job_id": job_id,
                "ceiling": MAX_CHANGED_CHUNKS_PER_JOB,
                "dropped_count": post_dropped,
            },
        )

    return ChangedSymbols(
        pre_change_hits=tuple(capped_pre_hits),
        post_change_hits=tuple(capped_post_hits),
        pre_dropped_count=pre_dropped,
        post_dropped_count=post_dropped,
    )


async def fetch_diff_content(
    *,
    diff_fetcher: DiffFetcher,
    tree_fetcher: RepoTreeFetcher,
    diff: Diff,
    installation_id: int,
    repo_full_name: str,
    head_sha: str,
    base_sha: str | None,
    changed_paths: Sequence[str],
    job_id: str,
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Fetches the ``(pre_content, post_content)`` dicts
    ``resolve_changed_symbols`` above consumes, per this module's own
    docstring and the plan's Phase 1 "New orchestration function" bullet.

    Post-change content is fetched via ``DiffFetcher.fetch_content`` at the
    PR's ``head_sha`` — raises the domain-owned
    ``ingestion.diff.DiffContentUnavailableError``/``DiffContentMalformedError``
    on failure.

    Pre-change content is fetched fresh at the PR's ``base_sha`` (per
    Assumption 1 — never read back from ``index_chunk``'s ``pr_number = 0``
    rows, which reflect the *current* default-branch tip, not necessarily
    this PR's actual merge-base) via
    ``RepoTreeFetcher.fetch_tree(commit_sha=base_sha)`` (raises
    ``RepoTreeFetchError``/``RepoTreeTruncatedError``) then
    ``RepoTreeFetcher.fetch_content(paths_with_blob_sha=...)`` (raises
    ``RepoTreeContentFetchError``) — reusing the exact "arbitrary path set
    at a commit" capability ``spec-indexing.md``'s own Unit B built for
    precisely this kind of second consumer.

    ``base_sha is None`` is checked explicitly *before* calling
    ``fetch_tree`` — never left to fail inside it: ``base_sha`` is optional
    at every layer (per Assumption 2) and can legitimately be ``None`` in
    production whenever GitHub's payload omits ``pull_request.base.sha``,
    not only during a deploy-migration window. When ``None``, this function
    skips the pre-change fetch entirely and returns an empty pre-change
    content dict — never calling ``fetch_tree(commit_sha=None)``, which
    would otherwise raise an untyped, uncaught error and (per US7) wrongly
    fail the whole job for what should be a benign missing-data case.

    None of this function's own raised exceptions are caught here — Phase
    5's ``ContextBuilderStep`` is the one place that catches them for
    graceful degradation (US7); this function's own fetch-at-two-commits
    behavior is proven by that phase's end-to-end integration test, not a
    unit test of this function in isolation (see this phase's own Tests
    subsection for why).
    """

    if base_sha is None:
        post_content = await diff_fetcher.fetch_content(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            diff=diff,
            paths=changed_paths,
        )
        logger.info(
            "context symbols: base_sha missing; skipping pre-change fetch",
            extra={"job_id": job_id, "repo_full_name": repo_full_name, "head_sha": head_sha},
        )
        return {}, post_content

    # Post-change content (at head_sha) and the base tree (at base_sha) are
    # independent reads against two different commits — neither's result is
    # needed to start the other — so they run concurrently rather than one
    # after the other, the same pattern `ContextBuilderStep._orchestrate`
    # already applies to its own caller-hits/similarity-hits branches.
    post_content, base_tree = await asyncio.gather(
        diff_fetcher.fetch_content(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            diff=diff,
            paths=changed_paths,
        ),
        tree_fetcher.fetch_tree(
            installation_id=installation_id, repo_full_name=repo_full_name, commit_sha=base_sha
        ),
    )
    tree_by_path = {entry.path: entry for entry in base_tree}
    entries = [tree_by_path[path] for path in changed_paths if path in tree_by_path]

    missing_paths = [path for path in changed_paths if path not in tree_by_path]
    if missing_paths:
        # A file absent from the base tree — the normal case for a file
        # newly added by this PR, which has no pre-change content at all.
        # Logged at info, never a warning: this is expected, not a defect.
        logger.info(
            "context symbols: some changed paths absent from base tree (newly added)",
            extra={
                "job_id": job_id,
                "repo_full_name": repo_full_name,
                "missing_count": len(missing_paths),
            },
        )

    if not entries:
        return {}, post_content

    pre_content = await tree_fetcher.fetch_content(
        installation_id=installation_id,
        repo_full_name=repo_full_name,
        paths_with_blob_sha=entries,
    )
    return pre_content, post_content
