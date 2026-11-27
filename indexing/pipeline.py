"""indexing.pipeline — the indexing job's own lightweight linear
orchestration.

Indexing's pipeline is a new, separate, plain linear sequence — never
``review/step.py``'s ``Step``/``StepRegistry`` framework promoted to a
shared location. Indexing's shape is a fixed sequence (resolve file set,
process each added/modified file, delete each removed file, advance the
cursor), not a DAG of independently-registrable named steps the way
review's macro/sub steps are.

``run_indexing_job`` is this module's one public entrypoint:
``apps/worker/dispatch.py`` calls it directly for every claimed ``INDEX``
job, wired with real adapters at the composition root
(``apps/worker/main.py``) via ``IndexingJobDependencies``.

**Per-file, sequential, four isolated stages (US10) — chunk, graph, embed,
upsert — chosen over a job-wide batched-embedding design,
which solved a throughput problem the spec never asked for while widening
a single bad chunk's blast radius to an entire run:** for each
added/modified file, in strict sequence, each wrapped
in its *own* ``try``/``except`` catching exactly the one typed exception
that stage's own domain function/Protocol method documents —
``chunk_file`` raises ``ChunkParseError``, ``extract_graph`` raises
``GraphExtractionError``, ``EmbeddingClient.embed`` raises
``EmbeddingError``, and the combined chunk+graph ``upsert_file``
call raises ``IndexingRepositoryError``. A failure at any stage logs
``file_path``/``language``/``stage``/the error detail (via ``logging``'s
``extra=`` structured-field mechanism — never string-interpolated into the
log template, since ``file_path`` originates from attacker-controlled
fork-PR input) and moves straight to the next file, skipping that file's
remaining stages entirely — never a partial commit of some but not all of
one file's own data. An exception outside these four typed exceptions (a
genuine bug) is deliberately left uncaught here and propagates to the
Worker's existing generic dispatch-loop handler
(``apps/worker/dispatch.py``'s own bug-resilience catch-all).

**Removed files are deleted via a separate loop** — distinct from the
added/modified loop, since a removed file has no content to
chunk/embed — calling ``IndexRepository.delete_file`` per path, each
wrapped in its own ``try``/``except IndexingRepositoryError``, logged with
``stage="delete"`` (never reusing ``"upsert"``, so a delete failure is
never misattributed).

**``set_last_indexed_commit`` is called only for ``pr_number == 0`` jobs**
(so a PR-scoped job never poisons the repo-level cursor), once, after every file in the
job has been processed (successfully or isolated-and-skipped) — never
incrementally per file — so a job that fails entirely before reaching this
point leaves the cursor at its prior value and the next push's delta
naturally reprocesses from the last known-good state.

**File-set resolution is the one place a whole-job-fatal failure is
expected and named:** ``RepoTreeTruncatedError``/``RepoTreeFetchError``
(from ``RepoTreeFetcher.fetch_tree``, US8 — a truncated response, or a
connectivity/malformed-response failure reaching the Git Trees API at
all, for either the current commit or, in the full-tree-diff fallback,
the previously-indexed commit, which may no longer be resolvable after a
force-push/history rewrite) and ``IndexingRepositoryError`` (from
``IndexCommitTracker``, both read and write sides) are caught around
file-set resolution and the final cursor advance, logged, and re-raised
as ``IndexingJobFailedError`` — the one typed, job-fatal exception
``apps/worker/dispatch.py`` catches the same way ``review.pipeline``'s
``PipelineFailedError`` is already caught for ``REVIEW`` jobs. Both of
``fetch_tree``'s own named exceptions are treated identically here —
neither is silently left to propagate as an uncaught "genuine bug", which
would otherwise leave the job stuck ``CLAIMED`` forever instead of
cleanly marked ``FAILED`` and naturally retried on the next push.

**The bulk ``fetch_content`` call is not job-fatal, unlike
``fetch_tree``** — ``RepoTreeFetcher.fetch_content`` fails its *whole*
batch rather than a silently partial result (never a silently
skipped/empty file, per that Protocol method's own docstring), but one
bad blob among many must not permanently stall the rest of a large push,
nor the repo's cursor forever (the next push's delta would keep re-diffing
against the same unadvanced cursor, re-including the same bad file, every
time). ``RepoTreeContentFetchError`` is caught around this one call and
every path in the requested batch is counted and logged as a per-file
*failure* (``stage="fetch_content"``, via the same ``_log_stage_failure``
every other stage uses) — loud, never folded into the quiet
``files_skipped`` count a genuine "no content available" caller mismatch
uses — while the job itself, the rest of that push's other files, and the
cursor advance all proceed normally.

**Cost ceilings (US6/US8's NFR), applied to the added/modified set only**
(per-push and full-run ceilings from ``spec-indexing.md``'s own Non-
Functional Requirements, mirroring ``review/prefilter.py``'s existing
two-pass file-count-then-aggregate-bytes pattern): the full-run ceiling
(20,000 files / 2GB) applies whenever this job used the full-tree-diff
fallback (``used_full_tree_fallback``); the incremental ceiling (500
files / 200MB) applies otherwise. Files beyond either ceiling are dropped
and logged at ``warning``, counted as skipped — never a job failure,
mirroring ``prefilter.py``'s own non-fatal drop pattern.

**No infra import anywhere in this module** (no ``httpx``/``sqlalchemy``/
``fastembed``) — every dependency is a domain-defined Protocol
(``RepoTreeFetcher``, ``EmbeddingClient``, ``IndexRepository``,
``IndexCommitTracker``), wired to real adapters only at the composition
root, per ``docs/rules/architecture.md``'s zero-infra-imports domain-
package rule.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass

from opentelemetry import metrics, trace
from pydantic import BaseModel, ConfigDict

from indexing._telemetry import start_span as _shared_start_span
from indexing._telemetry import suppress_telemetry_errors
from indexing.chunk import (
    ChunkLanguage,
    ChunkParseError,
    ChunkResult,
    chunk_file,
    language_for_path,
)
from indexing.embedding import EmbeddingClient, EmbeddingError
from indexing.graph import GraphExtractionError, GraphResult, extract_graph
from indexing.prefilter import filter_indexable_paths
from indexing.repository import IndexCommitTracker, IndexingRepositoryError, IndexRepository
from indexing.tree import (
    RepoTreeContentFetchError,
    RepoTreeEntry,
    RepoTreeFetcher,
    RepoTreeFetchError,
    RepoTreeTruncatedError,
)

logger = logging.getLogger(__name__)

# US9 (docs/specs/spec-observability.md's METRIC & SPAN CATALOG) — the OTel
# *API* import above, the same `tracer = trace.get_tracer(__name__)` /
# `meter = metrics.get_meter(__name__)` style already established across
# this codebase (adapter/telemetry/heartbeat.py, review/pipeline.py,
# review/context_builder.py) for importing OTel directly into domain code
# (mirroring how `logging.getLogger(__name__)` is already imported
# directly, per the spec's "Resolved decisions" — no `typing.Protocol`
# telemetry indirection). Unlike `review/pipeline.py` (whose module is
# itself literally named `review.pipeline`, so `__name__` already equals
# the catalog's tracer/meter name), this module is `indexing.pipeline` —
# `__name__` would be `"indexing.pipeline"`, not the catalog's
# `"review.indexing"`, so the catalog name is passed explicitly, the same
# `metrics.get_meter("review.context_builder")` shape
# `review/context_builder.py` already uses for an identical reason.
# `get_tracer`/`get_meter` both return no-op proxies until a composition
# root (`apps/webhook_api/main.py`/`apps/worker/main.py`) registers
# a real TracerProvider/MeterProvider — this module never constructs
# SDK/exporter infra itself.
tracer = trace.get_tracer("review.indexing")
meter = metrics.get_meter("review.indexing")

# Catalog names, verbatim (US9) — recorded once, at `run_indexing_job`'s
# completion, where `IndexingResult` is built. No attributes: the catalog
# gives no attribute list for these five instruments (unlike, e.g., US1's
# `{job_type}` or US4's `{severity, source}`), so none are invented here —
# a job-level aggregate count per completed job, not sliced further.
_files_processed_counter = meter.create_counter(
    "review.indexing.files_processed_total",
    description="Files successfully processed by an indexing job — chunked/embedded/upserted "
    "(added or modified files) or removed from the index (deleted files).",
)
_files_skipped_counter = meter.create_counter(
    "review.indexing.files_skipped_total",
    description="Files skipped by an indexing job (unsupported, ceiling-dropped, or no content).",
)
_files_failed_counter = meter.create_counter(
    "review.indexing.files_failed_total",
    description="Files that failed an isolated per-file stage during an indexing job.",
)
_chunks_created_counter = meter.create_counter(
    "review.indexing.chunks_created_total",
    description="Chunks created by an indexing job.",
)
_edges_created_counter = meter.create_counter(
    "review.indexing.edges_created_total",
    description="Call/import graph edges created by an indexing job.",
)
_duration_histogram = meter.create_histogram(
    "review.indexing.duration_ms",
    unit="ms",
    description="Wall-clock duration of one indexing job, in milliseconds.",
)


def _start_span(name: str) -> AbstractContextManager[trace.Span]:
    """This module's ``tracer`` bound into the shared
    ``indexing._telemetry.start_span`` helper — see that function's
    docstring for why the ``record_exception=False``/
    ``set_status_on_exception=False`` pair is centralized there instead of
    redefined per call site.
    """
    return _shared_start_span(tracer, name)


# Carried forward from spec-indexing.md's own Non-Functional Requirements
# ("Cost ceilings ... recommended starting defaults, tuned during
# implementation") — same status as review/prefilter.py's own ceiling
# constants: plain module constants, not operator-configurable bootstrap
# settings, per the "Could-have, not Must-have" distinction already
# established there.
INCREMENTAL_FILE_CEILING = 500
INCREMENTAL_BYTES_CEILING = 200_000_000  # ~200MB
FULL_RUN_FILE_CEILING = 20_000
FULL_RUN_BYTES_CEILING = 2_000_000_000  # ~2GB

# The job-level "files_failed / files_processed" warning-log threshold
# — a recommended default, overrideable, not a product-validated number.
FAILURE_RATIO_WARNING_THRESHOLD = 0.2

# Git object modes for a symlink / submodule gitlink entry, per
# indexing.tree.RepoTreeEntry's own docstring.
_SYMLINK_MODE = "120000"
_SUBMODULE_MODE = "160000"


class IndexingJobPayload(BaseModel):
    """Validated shape of an ``INDEX`` job's raw queue payload — built by
    ``apps/webhook_api/webhook_routes.py``'s ``_index_enqueue_payload``,
    per that module's own docstring. Parsed once, at this pipeline's own
    boundary, per architecture.md's "validate every boundary ... job
    payloads ... with pydantic models" rule — the same pattern
    ``review/prefilter.py``'s ``PrefilterInput`` already establishes for
    ``REVIEW`` jobs.

    ``pr_number`` is ``0`` for a ``push``-triggered (canonical,
    default-branch) job, the real PR number for a ``pull_request``-
    triggered job.
    """

    model_config = ConfigDict(frozen=True)

    org_id: uuid.UUID
    repo_id: uuid.UUID
    installation_id: int
    repo_full_name: str
    pr_number: int
    commit_sha: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    force_full_tree_fallback: bool = False


class IndexingResult(BaseModel):
    """``run_indexing_job``'s return value — every count the Observability
    NFR's job-level structured log line names, returned as a typed value
    (not only logged) so a caller/test can assert on it directly rather
    than parsing log output.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    repo_id: uuid.UUID
    pr_number: int
    commit_sha: str
    used_full_tree_fallback: bool
    files_processed: int
    files_skipped: int
    files_failed: int
    chunks_created: int
    edges_created: int
    duration_seconds: float


class IndexingJobFailedError(Exception):
    """Raised by ``run_indexing_job`` when the whole job cannot proceed — a
    file-set-resolution failure (``RepoTreeTruncatedError``/
    ``RepoTreeFetchError``) or a ``last_indexed_commit`` cursor read/write
    failure (``IndexingRepositoryError``) — as distinct from a per-file
    stage failure (chunk/graph/embed/upsert/delete), which is isolated to
    that one file per US10 and never raises this.

    Mirrors ``review.pipeline.PipelineFailedError``'s role for ``REVIEW``
    jobs: ``apps/worker/dispatch.py``'s dispatch loop catches this the same
    way, marking the job ``FAILED``.
    """

    def __init__(self, *, detail: str) -> None:
        super().__init__(f"indexing job failed: {detail}")
        self.detail = detail


@dataclass(frozen=True, slots=True)
class IndexingJobDependencies:
    """The domain-defined Protocols ``run_indexing_job`` depends on — wired
    to real adapters only at the composition root (``apps/worker/main.py``),
    per architecture.md's "wire concrete implementations only at the
    composition root" rule.
    """

    tree_fetcher: RepoTreeFetcher
    embedding_client: EmbeddingClient
    index_repository: IndexRepository
    commit_tracker: IndexCommitTracker


@dataclass(frozen=True, slots=True)
class _ResolvedFileSet:
    """The outcome of resolving which paths this job should actually
    process — either straight from the payload's own per-commit lists, or
    US8's full-tree-diff fallback — plus the current commit's full tree
    (keyed by path) every subsequent content-fetch lookup in this module
    needs.
    """

    added_or_modified: tuple[str, ...]
    removed: tuple[str, ...]
    used_full_tree_fallback: bool
    tree_by_path: dict[str, RepoTreeEntry]


def _diff_trees(
    current: tuple[RepoTreeEntry, ...], previous: tuple[RepoTreeEntry, ...]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """US8's full-tree-diff: compares two full tree listings by path and
    ``blob_sha`` (never by fetching and comparing real content) to derive
    the same (added, removed, modified) shape the payload's own per-commit
    lists carry in the non-fallback case.
    """
    current_by_path = {entry.path: entry.blob_sha for entry in current}
    previous_by_path = {entry.path: entry.blob_sha for entry in previous}

    added = tuple(path for path in current_by_path if path not in previous_by_path)
    removed = tuple(path for path in previous_by_path if path not in current_by_path)
    modified = tuple(
        path
        for path in current_by_path
        if path in previous_by_path and current_by_path[path] != previous_by_path[path]
    )
    return added, removed, modified


async def _fetch_current_tree_and_previous_sha(
    payload: IndexingJobPayload, deps: IndexingJobDependencies
) -> tuple[tuple[RepoTreeEntry, ...], str | None]:
    """Fetches the current commit's tree, plus (only on the full-tree-diff
    fallback path) the last indexed commit sha; ``None`` otherwise."""
    if not payload.force_full_tree_fallback:
        current_tree = await deps.tree_fetcher.fetch_tree(
            installation_id=payload.installation_id,
            repo_full_name=payload.repo_full_name,
            commit_sha=payload.commit_sha,
        )
        return current_tree, None
    # Neither call's arguments depend on the other's result — run them
    # concurrently rather than paying two sequential round-trips
    # (GitHub Trees API, then Postgres) on a path that's unconditional
    # for every `pull_request`-triggered job.
    current_tree, previous_commit_sha = await asyncio.gather(
        deps.tree_fetcher.fetch_tree(
            installation_id=payload.installation_id,
            repo_full_name=payload.repo_full_name,
            commit_sha=payload.commit_sha,
        ),
        deps.commit_tracker.get_last_indexed_commit(repo_id=payload.repo_id),
    )
    return current_tree, previous_commit_sha


async def _raw_changed_paths(
    payload: IndexingJobPayload,
    deps: IndexingJobDependencies,
    current_tree: tuple[RepoTreeEntry, ...],
    previous_commit_sha: str | None,
    tree_by_path: dict[str, RepoTreeEntry],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Returns (added, removed, modified) before reconciliation: the payload's
    own lists, or the full-tree diff when ``force_full_tree_fallback`` is set."""
    if payload.force_full_tree_fallback:
        if previous_commit_sha is None:
            # No prior index exists — the degenerate "every in-scope file
            # changed" full-run case, per spec-indexing.md's Rollout &
            # Release Safety section: no separate initial-index job type,
            # the normal incremental code path just sees a larger file set.
            added_paths: tuple[str, ...] = tuple(tree_by_path)
            removed_paths: tuple[str, ...] = ()
            modified_paths: tuple[str, ...] = ()
        else:
            previous_tree = await deps.tree_fetcher.fetch_tree(
                installation_id=payload.installation_id,
                repo_full_name=payload.repo_full_name,
                commit_sha=previous_commit_sha,
            )
            added_paths, removed_paths, modified_paths = _diff_trees(current_tree, previous_tree)
    else:
        added_paths = payload.added
        removed_paths = payload.removed
        modified_paths = payload.modified
    return added_paths, removed_paths, modified_paths


def _reconcile_changed_paths(
    added_paths: tuple[str, ...],
    removed_paths: tuple[str, ...],
    modified_paths: tuple[str, ...],
    tree_by_path: dict[str, RepoTreeEntry],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Returns (combined added/modified, reconciled removed) paths."""
    # Modified paths first: `_apply_cost_ceilings`'s file-count truncation
    # below keeps only the *leading* paths of this combined tuple when a
    # push exceeds the ceiling, and a modified path already has stale
    # indexed data from a prior push (actively wrong if never refreshed),
    # while a never-yet-seen added path simply has none yet (merely
    # incomplete) — a strictly better position to be dropped into if a
    # ceiling forces a choice between the two.
    combined_added_modified = tuple(dict.fromkeys((*modified_paths, *added_paths)))

    # A path can be deleted in one commit and re-added (or modified) in a
    # later commit of the same push — `_merged_push_file_lists` merges each
    # commit's own added/removed/modified lists independently and has no
    # way to net that across commits itself, so `removed_paths` can still
    # name a path that currently exists. Reconciled here against the one
    # source of truth for "does this path exist right now": a path present
    # in the current tree is never actually removed, no matter which
    # per-commit list first put it in `removed_paths` — left unreconciled,
    # the removed-file loop below would delete the very rows the
    # added/modified loop just wrote for it. (Already a no-op for the
    # full-tree-diff fallback branch above, whose own `removed`/`added` are
    # disjoint by construction.)
    removed_paths = tuple(path for path in removed_paths if path not in tree_by_path)

    # The mirror image of the reconciliation above: a path can equally be
    # added/modified in one commit and then removed in a later commit of
    # the same push, so `combined_added_modified` can still name a path
    # this same job's removed-file loop is about to delete below. Excluded
    # here — but *only* when it's actually in the now-reconciled
    # `removed_paths` above, never merely for being absent from the current
    # tree (a path that's absent from the tree but was never in
    # `removed_paths` at all is still a genuine `files_skipped` case, per
    # `test_path_absent_from_current_tree_is_skipped_exactly_once``'s own
    # regression coverage) — left unreconciled, the added/modified loop
    # below would count such a shared path as `files_skipped` (its own
    # `tree_by_path.get(path) is None` branch) *and* the removed-file loop
    # would separately count the very same path as `files_processed`,
    # double-counting one file across two outcome buckets in the job's own
    # summary/telemetry.
    removed_path_set = set(removed_paths)
    combined_added_modified = tuple(
        path for path in combined_added_modified if path not in removed_path_set
    )
    return combined_added_modified, removed_paths


async def _resolve_file_set(
    payload: IndexingJobPayload, deps: IndexingJobDependencies
) -> _ResolvedFileSet:
    """Resolves the file-change set this job should process: the payload's
    own added/modified/removed lists in the ordinary case, or US8's
    full-tree-diff fallback when ``force_full_tree_fallback`` is set
    (truncated/force-pushed ``push`` payloads, and every ``pull_request``-
    triggered job unconditionally — see
    ``apps/webhook_api/webhook_routes.py``'s ``_index_enqueue_payload``).

    The current commit's full tree is fetched unconditionally either way —
    even the non-fallback path needs it to resolve each added/modified
    path's real ``blob_sha`` for ``RepoTreeFetcher.fetch_content``'s own
    "arbitrary path set, not diff-scoped" contract, which takes
    ``RepoTreeEntry`` values, never bare paths.

    Every list returned here has already passed US9's pre-filter
    (``indexing.prefilter.filter_indexable_paths``) — an unsupported-
    language or ignored file never reaches any later stage.
    """
    current_tree, previous_commit_sha = await _fetch_current_tree_and_previous_sha(payload, deps)
    tree_by_path = {entry.path: entry for entry in current_tree}
    added_paths, removed_paths, modified_paths = await _raw_changed_paths(
        payload, deps, current_tree, previous_commit_sha, tree_by_path
    )

    combined_added_modified, removed_paths = _reconcile_changed_paths(
        added_paths, removed_paths, modified_paths, tree_by_path
    )

    return _ResolvedFileSet(
        added_or_modified=filter_indexable_paths(combined_added_modified),
        removed=filter_indexable_paths(removed_paths),
        used_full_tree_fallback=payload.force_full_tree_fallback,
        tree_by_path=tree_by_path,
    )


def _apply_cost_ceilings(
    paths: tuple[str, ...],
    tree_by_path: dict[str, RepoTreeEntry],
    *,
    full_run: bool,
    job_id: str,
) -> tuple[str, ...]:
    """US6/US8's cost ceilings, applied to the added/modified path set —
    mirrors ``review/prefilter.py``'s own two-pass materialization-ceiling-
    then-aggregate-bytes-ceiling pattern exactly (file-count ceiling first,
    then a running-total byte ceiling over the survivors), non-fatal,
    logged at ``warning`` with the dropped count either way.

    Deliberately duplicated, not imported or shared, for the same reason
    ``indexing/prefilter.py``'s own docstring already gives for its own
    duplication of ``review/prefilter.py``'s ignore-pattern constants: importing
    ``review`` package internals from ``indexing`` would cross a bounded-context boundary
    ``docs/rules/architecture.md``'s package-structure rule argues
    against, and moving this two-pass algorithm to a new shared location
    would recreate the ``common``/``shared``/``utils`` dump that same rule
    explicitly forbids. A ~15-line ceiling-drop algorithm duplicated
    across two files is a cheap, explicit trade-off, not a shared-module
    workaround — keep any future edit to one copy in sync with the other
    by hand, the same way ``indexing/prefilter.py``'s own constants are.
    """
    file_ceiling = FULL_RUN_FILE_CEILING if full_run else INCREMENTAL_FILE_CEILING
    bytes_ceiling = FULL_RUN_BYTES_CEILING if full_run else INCREMENTAL_BYTES_CEILING

    if len(paths) > file_ceiling:
        kept = paths[:file_ceiling]
        logger.warning(
            "index job: materialization file-count ceiling exceeded; dropping excess files",
            extra={
                "job_id": job_id,
                "ceiling": file_ceiling,
                "dropped_count": len(paths) - file_ceiling,
            },
        )
    else:
        kept = paths

    return _apply_bytes_ceiling(kept, tree_by_path, bytes_ceiling, job_id)


def _apply_bytes_ceiling(
    kept: tuple[str, ...],
    tree_by_path: dict[str, RepoTreeEntry],
    bytes_ceiling: int,
    job_id: str,
) -> tuple[str, ...]:
    """Second pass of ``_apply_cost_ceilings``: a running-total byte ceiling
    over the paths that survived the file-count ceiling (see that function's
    docstring for the rationale and the deliberate duplication)."""
    bytes_kept: list[str] = []
    running_total = 0
    dropped_bytes_count = 0
    for path in kept:
        entry = tree_by_path.get(path)
        size = entry.size if entry is not None and entry.size is not None else 0
        if running_total + size > bytes_ceiling:
            dropped_bytes_count += 1
            continue
        running_total += size
        bytes_kept.append(path)

    if dropped_bytes_count:
        logger.warning(
            "index job: aggregate-bytes ceiling exceeded; dropping excess files",
            extra={
                "job_id": job_id,
                "ceiling_bytes": bytes_ceiling,
                "dropped_count": dropped_bytes_count,
            },
        )

    return tuple(bytes_kept)


def _log_stage_failure(
    *, job_id: str, file_path: str, language: str | None, stage: str, error: BaseException
) -> None:
    """One shared log call for every per-file stage failure (chunk/graph/
    embed/upsert/delete) — structured fields only, per this module's own
    docstring on why ``file_path`` (attacker-controlled) is never
    string-interpolated into the log template itself.
    """
    logger.warning(
        "index job: file processing failed at stage",
        extra={
            "job_id": job_id,
            "file_path": file_path,
            "language": language,
            "stage": stage,
            "error": str(error),
        },
    )


async def _embed_and_upsert(
    path: str,
    language: ChunkLanguage,
    chunk_result: ChunkResult,
    graph_result: GraphResult,
    *,
    parsed: IndexingJobPayload,
    deps: IndexingJobDependencies,
    job_id: str,
) -> bool:
    """The embed and upsert stages of ``_process_one_added_or_modified_file``
    (see its docstring and this module's own docstring for the per-stage
    isolation design). Returns ``False`` if either stage failed (already
    logged here), ``True`` once the file is upserted.
    """
    try:
        texts = tuple(chunk.content for chunk in chunk_result.chunks)
        embeddings = await asyncio.to_thread(deps.embedding_client.embed, texts) if texts else ()
    except EmbeddingError as exc:
        _log_stage_failure(
            job_id=job_id, file_path=path, language=language, stage="embed", error=exc
        )
        return False

    try:
        await deps.index_repository.upsert_file(
            repo_id=parsed.repo_id,
            pr_number=parsed.pr_number,
            file_path=path,
            chunks=chunk_result.chunks,
            embeddings=embeddings,
            nodes=graph_result.nodes,
            edges=graph_result.edges,
        )
    except IndexingRepositoryError as exc:
        _log_stage_failure(
            job_id=job_id, file_path=path, language=language, stage="upsert", error=exc
        )
        return False

    return True


async def _process_one_added_or_modified_file(
    path: str,
    language: ChunkLanguage,
    content: bytes,
    *,
    parsed: IndexingJobPayload,
    deps: IndexingJobDependencies,
    job_id: str,
) -> tuple[int, int] | None:
    """Runs one file's four isolated stages (chunk, graph, embed, upsert)
    in strict sequence — see this module's own docstring for the full
    per-stage isolation design. Returns ``(chunks_created, edges_created)``
    on success, or ``None`` if any stage failed (already logged by that
    stage's own ``except`` block) — the caller moves on to the next file
    either way, never retrying or partially committing within this file.

    ``chunk_file``/``extract_graph`` (CPU-bound Tree-sitter parsing) and
    ``EmbeddingClient.embed`` (CPU-bound local ONNX inference) each run via
    ``asyncio.to_thread`` — synchronous, blocking calls kept off the
    Worker's single event loop, the same mechanism
    ``adapter/github/auth.py``'s RS256 signing and
    ``adapter/analyzers/subprocess_runner.py``'s analyzer sub-steps already
    use for this exact reason.
    """
    try:
        chunk_result = await asyncio.to_thread(chunk_file, language, path, content)
    except ChunkParseError as exc:
        _log_stage_failure(
            job_id=job_id, file_path=path, language=language, stage="chunk", error=exc
        )
        return None

    try:
        graph_result = await asyncio.to_thread(extract_graph, language, path, chunk_result.tree)
    except GraphExtractionError as exc:
        _log_stage_failure(
            job_id=job_id, file_path=path, language=language, stage="graph", error=exc
        )
        return None

    if not await _embed_and_upsert(
        path, language, chunk_result, graph_result, parsed=parsed, deps=deps, job_id=job_id
    ):
        return None

    return len(chunk_result.chunks), len(graph_result.edges)


@dataclass
class _Tally:
    """Mutable per-job file/chunk/edge counters threaded through the stage helpers."""

    files_processed: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    chunks_created: int = 0
    edges_created: int = 0


async def _fetch_contents(
    entries: list[RepoTreeEntry],
    *,
    parsed: IndexingJobPayload,
    deps: IndexingJobDependencies,
    job_id: str,
) -> tuple[dict[str, bytes], RepoTreeContentFetchError | None]:
    content_by_path: dict[str, bytes] = {}
    content_fetch_error: RepoTreeContentFetchError | None = None
    if not entries:
        return content_by_path, content_fetch_error
    with _start_span("review.indexing.stage.fetch_content"):
        try:
            content_by_path = await deps.tree_fetcher.fetch_content(
                installation_id=parsed.installation_id,
                repo_full_name=parsed.repo_full_name,
                paths_with_blob_sha=entries,
            )
        except RepoTreeContentFetchError as exc:
            # `fetch_content` fails the *whole* batch rather than a silently
            # partial result (see `RepoTreeContentFetchError`'s own docstring:
            # never a silently skipped/empty file). Caught here, loudly, as a
            # per-file *failure* for every path in this batch (never folded into
            # the quiet `files_skipped` count) so one bad file in a large push
            # doesn't leave the whole job permanently stuck: the cursor still
            # advances past this push.
            content_fetch_error = exc
            logger.error(
                "index job: batch content fetch failed",
                extra={
                    "job_id": job_id,
                    "repo_id": str(parsed.repo_id),
                    "path_count": len(entries),
                    "error": str(exc),
                },
            )
    return content_by_path, content_fetch_error


async def _delete_symlink_or_submodule(
    path: str,
    tally: _Tally,
    *,
    parsed: IndexingJobPayload,
    deps: IndexingJobDependencies,
    job_id: str,
) -> None:
    # A path can flip from a regular file to a symlink/submodule between pushes
    # — any chunk/graph rows indexed from when it *was* a regular file must be
    # cleaned up here (idempotent: a no-op delete for a path that was never a
    # regular file), or they persist indefinitely, since no other loop ever
    # revisits a path that isn't in `file_set.removed`.
    try:
        await deps.index_repository.delete_file(
            repo_id=parsed.repo_id, pr_number=parsed.pr_number, file_path=path
        )
    except IndexingRepositoryError as exc:
        tally.files_failed += 1
        _log_stage_failure(job_id=job_id, file_path=path, language=None, stage="delete", error=exc)
        return
    tally.files_skipped += 1


def _record_missing_content(
    path: str, content_fetch_error: RepoTreeContentFetchError | None, tally: _Tally, *, job_id: str
) -> None:
    if content_fetch_error is not None:
        tally.files_failed += 1
        _log_stage_failure(
            job_id=job_id,
            file_path=path,
            language=None,
            stage="fetch_content",
            error=content_fetch_error,
        )
        return
    tally.files_skipped += 1
    logger.info(
        "index job: file skipped (no content available)",
        extra={"job_id": job_id, "file_path": path},
    )


async def _process_files(
    added_or_modified: Sequence[str],
    file_set: _ResolvedFileSet,
    content_by_path: dict[str, bytes],
    content_fetch_error: RepoTreeContentFetchError | None,
    tally: _Tally,
    *,
    parsed: IndexingJobPayload,
    deps: IndexingJobDependencies,
    job_id: str,
) -> None:
    for path in added_or_modified:
        entry = file_set.tree_by_path.get(path)
        if entry is None:
            tally.files_skipped += 1
            continue
        if entry.mode in (_SYMLINK_MODE, _SUBMODULE_MODE):
            await _delete_symlink_or_submodule(path, tally, parsed=parsed, deps=deps, job_id=job_id)
            continue
        content = content_by_path.get(path)
        if content is None:
            _record_missing_content(path, content_fetch_error, tally, job_id=job_id)
            continue

        language = language_for_path(path)
        if language is None:  # pragma: no cover - defensive; prefilter already guarantees this
            tally.files_skipped += 1
            continue

        outcome = await _process_one_added_or_modified_file(
            path, language, content, parsed=parsed, deps=deps, job_id=job_id
        )
        if outcome is None:
            tally.files_failed += 1
            continue

        chunk_count, edge_count = outcome
        tally.chunks_created += chunk_count
        tally.edges_created += edge_count
        tally.files_processed += 1


async def _delete_removed(
    removed: tuple[str, ...],
    tally: _Tally,
    *,
    parsed: IndexingJobPayload,
    deps: IndexingJobDependencies,
    job_id: str,
) -> None:
    for path in removed:
        try:
            await deps.index_repository.delete_file(
                repo_id=parsed.repo_id, pr_number=parsed.pr_number, file_path=path
            )
        except IndexingRepositoryError as exc:
            _log_stage_failure(
                job_id=job_id, file_path=path, language=None, stage="delete", error=exc
            )
            tally.files_failed += 1
            continue
        tally.files_processed += 1


async def _advance_cursor(
    *, parsed: IndexingJobPayload, deps: IndexingJobDependencies, job_id: str
) -> None:
    try:
        await deps.commit_tracker.set_last_indexed_commit(
            repo_id=parsed.repo_id, commit_sha=parsed.commit_sha
        )
    except IndexingRepositoryError as exc:
        logger.error(
            "index job: failed to advance last-indexed-commit cursor",
            extra={"job_id": job_id, "repo_id": str(parsed.repo_id), "error": str(exc)},
        )
        raise IndexingJobFailedError(detail=str(exc)) from exc


async def _resolve_file_set_or_fail(
    parsed: IndexingJobPayload, deps: IndexingJobDependencies, *, job_id: str
) -> _ResolvedFileSet:
    with _start_span("review.indexing.stage.resolve_file_set"):
        try:
            return await _resolve_file_set(parsed, deps)
        except (RepoTreeTruncatedError, RepoTreeFetchError, IndexingRepositoryError) as exc:
            logger.error(
                "index job: file-set resolution failed",
                extra={
                    "job_id": job_id,
                    "repo_id": str(parsed.repo_id),
                    "pr_number": parsed.pr_number,
                    "commit_sha": parsed.commit_sha,
                    "error": str(exc),
                },
            )
            raise IndexingJobFailedError(detail=str(exc)) from exc


def _record_completion_metrics(
    result: IndexingResult, *, parsed: IndexingJobPayload, job_id: str
) -> None:
    # Guarded: the job itself already completed successfully by this
    # point (`result` is fully built), so a metrics-recording failure
    # must never propagate as an unhandled exception out of this
    # function — apps/worker/dispatch.py has no distinct handling for
    # that and would mark an otherwise-successful job FAILED.
    with suppress_telemetry_errors(
        logger,
        "index job: US9 completion metrics recording failed (job outcome unaffected)",
        job_id=job_id,
        repo_id=str(parsed.repo_id),
    ):
        _files_processed_counter.add(result.files_processed)
        _files_skipped_counter.add(result.files_skipped)
        _files_failed_counter.add(result.files_failed)
        _chunks_created_counter.add(result.chunks_created)
        _edges_created_counter.add(result.edges_created)
        _duration_histogram.record(result.duration_seconds * 1000)


def _log_completion(
    parsed: IndexingJobPayload,
    tally: _Tally,
    file_set: _ResolvedFileSet,
    duration_seconds: float,
    *,
    job_id: str,
) -> None:
    files_processed, files_failed = tally.files_processed, tally.files_failed
    ratio_exceeded = files_failed > 0 and (
        files_processed == 0 or (files_failed / files_processed) > FAILURE_RATIO_WARNING_THRESHOLD
    )
    log_fn = logger.warning if ratio_exceeded else logger.info
    log_fn(
        "index job completed",
        extra={
            "job_id": job_id,
            "org_id": str(parsed.org_id),
            "repo_id": str(parsed.repo_id),
            "pr_number": parsed.pr_number,
            "commit_sha": parsed.commit_sha,
            "used_full_tree_fallback": file_set.used_full_tree_fallback,
            "files_processed": files_processed,
            "files_skipped": tally.files_skipped,
            "files_failed": files_failed,
            "chunks_created": tally.chunks_created,
            "edges_created": tally.edges_created,
            "duration_seconds": duration_seconds,
        },
    )


def _select_entries(
    file_set: _ResolvedFileSet, *, job_id: str
) -> tuple[tuple[str, ...], list[RepoTreeEntry]]:
    added_or_modified = _apply_cost_ceilings(
        file_set.added_or_modified,
        file_set.tree_by_path,
        full_run=file_set.used_full_tree_fallback,
        job_id=job_id,
    )
    entries = [
        file_set.tree_by_path[path] for path in added_or_modified if path in file_set.tree_by_path
    ]
    # Paths absent from tree_by_path are counted in files_skipped by the
    # per-file loop below (its own `entry is None` branch) — do not also
    # count them here, or they are double-counted.
    return added_or_modified, entries


def _tag_root_span(root_span: trace.Span, parsed: IndexingJobPayload, *, job_id: str) -> None:
    with suppress_telemetry_errors(
        logger,
        "root span attribute recording failed (indexing job unaffected)",
        job_id=job_id,
    ):
        root_span.set_attribute("review.indexing.job.id", job_id)
        root_span.set_attribute("review.indexing.repo.id", str(parsed.repo_id))
        root_span.set_attribute("review.indexing.org.id", str(parsed.org_id))


def _build_result(
    parsed: IndexingJobPayload,
    tally: _Tally,
    file_set: _ResolvedFileSet,
    duration_seconds: float,
    *,
    job_id: str,
) -> IndexingResult:
    return IndexingResult(
        job_id=job_id,
        repo_id=parsed.repo_id,
        pr_number=parsed.pr_number,
        commit_sha=parsed.commit_sha,
        used_full_tree_fallback=file_set.used_full_tree_fallback,
        files_processed=tally.files_processed,
        files_skipped=tally.files_skipped,
        files_failed=tally.files_failed,
        chunks_created=tally.chunks_created,
        edges_created=tally.edges_created,
        duration_seconds=duration_seconds,
    )


# Run one ``INDEX`` job to completion — see this module's own
# docstring for the full design. ``job_id`` is the claimed
# ``job_queue`` row's own id (``apps/worker/dispatch.py``'s ``job.id``),
# threaded through explicitly for structured logging, the same role
# ``review.step.StepContext.job_id`` plays for ``REVIEW`` jobs.
#
# Raises ``IndexingJobFailedError`` on a whole-job-fatal failure (file-set
# resolution, or the final cursor advance) — never for a per-file stage
# failure, which is isolated per US10 and reflected only in the returned
# ``IndexingResult.files_failed`` count and this job's own completion log
# line.
#
# Wrapped in a root span, ``"review.indexing.job.run"`` (US9, catalog
# verbatim) — one per call, tagged with ``review.indexing.job.id``/
# ``review.indexing.repo.id``/``review.indexing.org.id`` (IDs only, per
# the Privacy/Compliance NFR's fixed allowlist — never a file path, a
# diff, or an exception's own text). Each real stage boundary this
# function's own body already has — file-set resolution, the bulk
# content fetch, the per-file added/modified loop, the removed-file
# delete loop, and the (conditional) cursor advance — gets its own child
# span, ``"review.indexing.stage.{name}"``. Deliberately **not** one span
# per per-file sub-stage (chunk/graph/embed/upsert, this module's own
# ``_process_one_added_or_modified_file``): a job can touch up to
# ``FULL_RUN_FILE_CEILING`` (20,000) files, and a span per sub-stage per
# file would multiply that by four, working against the Performance
# NFR's own hot-path-overhead ceiling for no query this PRD's shipped
# dashboard needs — the five job-level counters below already give
# per-stage-outcome visibility (files processed/skipped/failed,
# chunks/edges created) without per-file span volume. ``record_exception``/
# ``set_status_on_exception`` are disabled on every span here, the same
# Privacy/Compliance reason ``review.pipeline``'s own root/child spans
# disable them (a caught exception's ``str()`` — which can carry
# attacker-controlled ``file_path``/error detail — must never become span
# data).
#
# Records the five US9 job-completion counters/histogram (module-level
# ``_files_processed_counter`` etc.) at the same point ``IndexingResult``
# is built, from that same result's own fields — never attributed
# (the catalog gives no attribute list for these instruments, unlike,
# e.g., US1's ``{job_type}``), so this is one job-level data point per
# counter per job run.
#
async def run_indexing_job(
    payload: dict[str, object], deps: IndexingJobDependencies, *, job_id: str
) -> IndexingResult:
    started = time.monotonic()
    parsed = IndexingJobPayload.model_validate(payload)

    with _start_span("review.indexing.job.run") as root_span:
        _tag_root_span(root_span, parsed, job_id=job_id)

        file_set = await _resolve_file_set_or_fail(parsed, deps, job_id=job_id)

        added_or_modified, entries = _select_entries(file_set, job_id=job_id)
        files_skipped = len(file_set.added_or_modified) - len(added_or_modified)

        content_by_path, content_fetch_error = await _fetch_contents(
            entries, parsed=parsed, deps=deps, job_id=job_id
        )

        tally = _Tally(files_skipped=files_skipped)
        with _start_span("review.indexing.stage.process_files"):
            await _process_files(
                added_or_modified,
                file_set,
                content_by_path,
                content_fetch_error,
                tally,
                parsed=parsed,
                deps=deps,
                job_id=job_id,
            )

        with _start_span("review.indexing.stage.delete_removed_files"):
            await _delete_removed(file_set.removed, tally, parsed=parsed, deps=deps, job_id=job_id)

        if parsed.pr_number == 0:
            with _start_span("review.indexing.stage.advance_cursor"):
                await _advance_cursor(parsed=parsed, deps=deps, job_id=job_id)

        duration_seconds = time.monotonic() - started
        result = _build_result(parsed, tally, file_set, duration_seconds, job_id=job_id)

        _record_completion_metrics(result, parsed=parsed, job_id=job_id)
        _log_completion(parsed, tally, file_set, duration_seconds, job_id=job_id)

        return result
