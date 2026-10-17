"""indexing.repository — the storage-boundary Protocols for the indexing
pipeline, per the indexing delivery plan's Phase 6 section (Unit F).

**Two Protocols, not one 5-method Protocol (per Assumption 13, corrected
during an independent architecture review):** ``docs/rules/architecture.md``
caps a ``Protocol`` at 1-3 methods, defined at the consumer, requiring a new
ADR for any deviation — an earlier draft's single ``IndexingRepository``
Protocol had five methods with no such ADR.

- ``IndexRepository`` (``upsert_file``, ``delete_file`` — 2 methods) is the
  **per-job write path**: what Phase 7's pipeline calls once per
  added/modified/removed file while a job is running. ``upsert_file``
  combines a file's chunk and graph writes into one call, not two, so the
  concrete adapter can commit both in a single transaction (see
  ``upsert_file``'s own docstring) — a prior two-method split
  (``upsert_chunks``/``upsert_graph``) let a failure between the two commits
  leave a file's chunk rows durably written with no matching graph rows,
  contradicting ``indexing/pipeline.py``'s own documented
  never-a-partial-commit guarantee.
- ``IndexCommitTracker`` (``get_last_indexed_commit``,
  ``set_last_indexed_commit`` — 2 methods) is the **per-repo cursor**
  consulted *before* a job's write path even runs, to decide
  incremental-vs-full-tree-fallback (US8), and advanced once, after a
  ``pr_number == 0`` job's files are all processed (Phase 7's own ordering
  rule — never incrementally per file, and never at all for a
  ``pull_request``-triggered job, per Assumption 9's second, cursor-level
  poisoning fix).

Both are implemented by ``adapter/storage/indexing_repository.py``'s
concrete ``IndexingRepository`` class; ``indexing/pipeline.py`` (Phase 7)
depends on each narrow interface it actually needs, never the concrete
class directly.

No infra import anywhere in this module (no ``sqlalchemy``/``asyncpg``),
per ``docs/rules/architecture.md``'s zero-infra-imports domain-package
rule — the same reasoning already stated by ``indexing/tree.py``'s
``RepoTreeFetcher`` and ``indexing/embedding.py``'s ``EmbeddingClient``.

Every ``IndexRepository`` method raises ``IndexingRepositoryError`` — never
a raw ``sqlalchemy``/``asyncpg`` exception — on a Postgres-level failure,
per Assumption 14. The concrete implementation applies the same
translation to ``IndexCommitTracker``'s methods too, for the same reason
(a real Postgres failure should never leak a driver-level exception type
across this package boundary), even though Assumption 14's own wording
names ``IndexRepository`` specifically.

``repo_id``/``pr_number`` are passed explicitly by the caller on every
method here, never read off a ``Chunk``/``GraphNode``'s own (currently
unpopulated) ``repo_id`` field — Phase 3/5's own docstrings already say
``chunk_file``/``extract_graph`` never populate it (a single-file, no-job-
context pure function has no repo identity to put there); the job-level
identity a write actually needs is supplied by whichever caller already
has it (Phase 7's pipeline, or a test), the same way ``RepoTreeFetcher``'s
own methods take ``installation_id``/``repo_full_name`` explicitly rather
than expecting it embedded in a ``RepoTreeEntry``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol

from indexing.chunk import Chunk
from indexing.clone import CloneFileIndex
from indexing.embedding import EmbeddingVector
from indexing.graph import GraphEdge, GraphNode


class IndexingRepositoryError(Exception):
    """Raised by any ``IndexRepository``/``IndexCommitTracker`` method on a
    real Postgres-level failure (a constraint violation, a dropped
    connection mid-write, etc.) — never a raw ``sqlalchemy``/``asyncpg``
    exception, per architecture.md's exception-type rule and the plan's
    Assumption 14.
    """

    def __init__(self, *, operation: str, detail: str) -> None:
        super().__init__(f"index repository operation {operation!r} failed: {detail}")
        self.operation = operation
        self.detail = detail


class IndexRepository(Protocol):
    """The per-job write path Phase 7's pipeline calls once per
    added/modified/removed file, per Assumption 9's ``pr_number`` row-
    scoping and US4/US6/US7. Implemented by
    ``adapter/storage/indexing_repository.py``'s ``IndexingRepository``.
    """

    async def upsert_file(
        self,
        *,
        repo_id: uuid.UUID,
        pr_number: int,
        file_path: str,
        chunks: Sequence[Chunk],
        embeddings: Sequence[EmbeddingVector],
        nodes: Sequence[GraphNode],
        edges: Sequence[GraphEdge],
        clone_index: CloneFileIndex | None = None,
    ) -> None:
        """Upsert one file's own chunks *and* graph definitions in a single
        atomic write, scoped by ``repo_id``/``pr_number`` — one method, not
        two, precisely so the concrete adapter can commit both in one
        transaction (per ``indexing/pipeline.py``'s own documented
        never-a-partial-commit-of-one-file's-own-data guarantee).

        Chunks: upserts ``chunks`` (and each chunk's own ``embeddings[i]``,
        matched by position — ``len(chunks)`` must equal ``len(embeddings)``,
        and every ``chunk.file_path`` must equal ``file_path``) into
        ``index_chunk``. Idempotent on ``(repo_id, pr_number, file_path,
        chunk_index)`` (per US6/US11 — a retried or redelivered job for the
        same file overwrites cleanly rather than duplicating). Also deletes
        any pre-existing row for ``file_path`` whose ``chunk_index`` is
        absent from this call's own ``chunks`` — a real delete-then-insert
        step, not just an upsert, since a file's chunk count can shrink
        between pushes, down to and including zero (``chunks=()``, e.g. a
        file edited to become empty/whitespace-only — ``file_path`` is
        taken explicitly, rather than derived from ``chunks``, precisely so
        this all-deleted case still knows which file's stale rows to
        remove).

        Graph: upserts one file's own definitions (``nodes``, idempotent on
        ``(repo_id, pr_number, file_path, symbol_name)``) into
        ``index_graph_node``, per US5. Also deletes any pre-existing node
        for ``file_path`` whose ``symbol_name`` is absent from this call's
        own ``nodes`` (a symbol renamed or removed within a file that
        itself still exists), cascading to that node's own edges.
        ``file_path`` is taken explicitly (never derived from ``nodes``),
        the same as for chunks, precisely so this cleanup still runs when
        ``nodes`` is empty (e.g. a file edited down to zero definitions) —
        every ``node.file_path`` must equal ``file_path``. For every node in
        ``nodes``, deletes that node's prior outgoing edges before inserting
        the freshly-extracted set in ``edges`` — a real delete-then-insert
        step, not just an upsert, since a symbol's outgoing edges can shrink
        between pushes (per the plan's Phase 6 section).

        Clone data (``docs/plans/plan-phase-10.md`` Phase 2): ``clone_index``
        is the file's ``indexing.clone.CloneFileIndex`` (per-token hashes,
        line map, sampled fingerprints), replaced delete-then-insert in the
        **same transaction** as the chunks and graph. ``None`` means "no clone
        data for this file" — the file's existing clone rows are deleted
        rather than left stale (a tokenization failure for one file must not
        cost it its chunks, but must also never leave fingerprints for code
        that has since changed). Defaults to ``None`` so a caller that
        predates clone detection keeps working. Every clone row is keyed on
        ``(repo_id, pr_number, file_path)``, so a PR-scoped call can never
        touch the canonical ``pr_number = 0`` rows.

        Raises ``IndexingRepositoryError`` on a Postgres-level failure —
        either all of the chunk, graph and clone writes land, or none does.
        """
        ...

    async def delete_file(self, *, repo_id: uuid.UUID, pr_number: int, file_path: str) -> int:
        """Delete every chunk, graph node and clone row keyed to ``(repo_id,
        pr_number, file_path)``, per US7. Every graph edge whose *source*
        node was just deleted is removed too (via the schema's own
        ``ondelete=CASCADE`` on ``index_graph_edge.source_node_id`` — not a
        second explicit delete this method issues itself); an edge whose
        *target* was one of this file's now-deleted symbols is left in
        place as a dangling reference, per US7's own text. Returns the
        number of clone fingerprint rows removed (for the
        ``clone_fingerprints_deleted_total`` metric). Raises
        ``IndexingRepositoryError`` on a Postgres-level failure.
        """
        ...


class IndexCommitTracker(Protocol):
    """The per-repo cursor Phase 7's pipeline reads before a job's write
    path runs (to decide incremental-vs-full-tree-fallback, US8) and
    advances once a ``pr_number == 0`` job's files are all processed (per
    Assumption 9's cursor-level poisoning fix — never for a
    ``pull_request``-triggered job). Implemented by
    ``adapter/storage/indexing_repository.py``'s ``IndexingRepository``.
    """

    async def get_last_indexed_commit(self, *, repo_id: uuid.UUID) -> str | None:
        """The commit SHA ``repo_id``'s default branch was last indexed
        at, or ``None`` if it has never been indexed (the same "``NULL``
        means not yet resolved" pattern ``repo.installation_id`` already
        uses). Raises ``IndexingRepositoryError`` on a Postgres-level
        failure.
        """
        ...

    async def set_last_indexed_commit(self, *, repo_id: uuid.UUID, commit_sha: str) -> None:
        """Advance ``repo_id``'s stored cursor to ``commit_sha``. Raises
        ``IndexingRepositoryError`` on a Postgres-level failure.
        """
        ...


class CloneBackfillTracker(Protocol):
    """The per-repo clone-fingerprint backfill state
    (``docs/plans/plan-phase-10.md`` Phase 2, Assumption 5) — a separate
    Protocol from ``IndexCommitTracker`` so each stays within
    ``architecture.md``'s 1-3 method cap. Implemented by
    ``adapter/storage/indexing_repository.py``'s ``IndexingRepository``.
    """

    async def get_clone_fingerprint_version(self, *, repo_id: uuid.UUID) -> int:
        """The clone-fingerprint schema version ``repo_id``'s index was last
        fully backfilled at (``0`` until a backfill completes). Raises
        ``IndexingRepositoryError`` on a Postgres-level failure, or if the
        repo does not exist.
        """
        ...

    async def advance_cursor_with_clone_version(
        self,
        *,
        repo_id: uuid.UUID,
        commit_sha: str,
        expected_previous_commit: str | None,
        clone_version: int,
    ) -> bool:
        """Optimistic-concurrency write used only by a backfill job: set the
        last-indexed-commit cursor to ``commit_sha`` **and** the clone
        version to ``clone_version`` in one conditional ``UPDATE``, applied
        only if the cursor still equals ``expected_previous_commit`` (the
        value this job started from; ``None`` matches an unset cursor).
        Returns ``False`` — changing nothing — when another job advanced the
        cursor first, so a job that lost the race neither moves the cursor
        backwards nor stamps a version over rows another job wrote. Raises
        ``IndexingRepositoryError`` on a Postgres-level failure.
        """
        ...
