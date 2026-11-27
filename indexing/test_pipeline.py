"""Integration tests for `indexing.pipeline.run_indexing_job`.

Real Postgres via `testcontainers-python` (`migrated_engine`, per
`docs/rules/testing.md`'s mandatory DB-integration rule) with a real
`adapter.storage.indexing_repository.IndexingRepository` — this is the one
dedicated Integration-tier test where US3 (chunking) and US5 (graph
extraction) get their mandatory `testing.md` integration coverage,
exercised as part of running a real end-to-end job, alongside US6 (delta), US7 (deletion), US8
(full-tree-diff fallback), US9 (prefilter), US10 (per-stage isolation), the `pr_number ==
0`-only commit-advance rule, the cost-ceiling scenario, the
`files_failed`-ratio warning-log threshold, and the whole-job
Postgres-failure/resume case.

`indexing.conftest.FakeRepoTreeFetcher` returns the same tree regardless of
`commit_sha` — fine for every non-fallback test here, but US8's fallback
needs two genuinely different trees keyed by two different commits, so this
file defines its own one-off `_MultiCommitTreeFetcher`, per `testing.md`'s
"fakes over mocks" convention and `review`'s own established pattern of
per-test-file one-off fakes for a need specific to one file.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import indexing.pipeline as pipeline_module
from adapter.storage.engine import create_session_factory, create_storage_engine
from adapter.storage.indexing_repository import IndexingRepository
from adapter.storage.models import IndexChunkRow, IndexGraphEdgeRow, IndexGraphNodeRow, Org, Repo
from indexing.chunk import Chunk, chunk_file
from indexing.conftest import FakeEmbeddingClient
from indexing.graph import GraphExtractionError, GraphResult
from indexing.pipeline import IndexingJobDependencies, IndexingJobFailedError, run_indexing_job
from indexing.repository import IndexingRepositoryError
from indexing.tree import (
    RepoTreeContentFetchError,
    RepoTreeEntry,
    RepoTreeFetcher,
    RepoTreeFetchError,
    RepoTreeTruncatedError,
)

pytestmark = pytest.mark.integration

_COMMIT_1 = "1" * 40
_COMMIT_2 = "2" * 40

_HELPER_TS = b"function helper(): void {}\n"
_CALLER_TS = b"function caller(): void {\n  helper();\n}\n"


@dataclass
class _MultiCommitTreeFetcher:
    """A `RepoTreeFetcher` fake that returns a different tree per
    `commit_sha` (see this module's own docstring for why
    `indexing.conftest.FakeRepoTreeFetcher` can't stand in for US8's
    two-commit diff scenario). `content` is shared across every commit
    (keyed by path only) since only the *current* commit's content is ever
    actually fetched by the pipeline.
    """

    trees: dict[str, tuple[RepoTreeEntry, ...]]
    content: dict[str, bytes] = field(default_factory=dict)
    truncated_for: frozenset[str] = frozenset()
    unreachable_for: frozenset[str] = frozenset()
    fail_content_fetch: bool = False
    fetch_tree_calls: list[str] = field(default_factory=list)

    async def fetch_tree(
        self, *, installation_id: int, repo_full_name: str, commit_sha: str
    ) -> tuple[RepoTreeEntry, ...]:
        self.fetch_tree_calls.append(commit_sha)
        if commit_sha in self.truncated_for:
            raise RepoTreeTruncatedError(repo_full_name=repo_full_name, commit_sha=commit_sha)
        if commit_sha in self.unreachable_for:
            raise RepoTreeFetchError(
                repo_full_name=repo_full_name, commit_sha=commit_sha, detail="forced failure"
            )
        return self.trees[commit_sha]

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        paths_with_blob_sha: Sequence[RepoTreeEntry],
    ) -> dict[str, bytes]:
        if self.fail_content_fetch:
            raise RepoTreeContentFetchError(repo_full_name=repo_full_name, detail="forced failure")
        paths = tuple(entry.path for entry in paths_with_blob_sha)
        return {path: self.content[path] for path in paths if path in self.content}


_: type[RepoTreeFetcher] = _MultiCommitTreeFetcher


def _entry(path: str, content: bytes, *, blob_sha: str | None = None) -> RepoTreeEntry:
    return RepoTreeEntry(
        path=path,
        mode="100644",
        blob_sha=blob_sha or f"sha-{path}-{len(content)}",
        size=len(content),
    )


def _payload(
    *,
    repo_id: uuid.UUID,
    commit_sha: str = _COMMIT_1,
    pr_number: int = 0,
    added: Sequence[str] = (),
    removed: Sequence[str] = (),
    modified: Sequence[str] = (),
    force_full_tree_fallback: bool = False,
) -> dict[str, object]:
    return {
        "org_id": str(uuid.uuid4()),
        "repo_id": str(repo_id),
        "installation_id": 1,
        "repo_full_name": "acme/widgets",
        "pr_number": pr_number,
        "commit_sha": commit_sha,
        "added": list(added),
        "removed": list(removed),
        "modified": list(modified),
        "force_full_tree_fallback": force_full_tree_fallback,
    }


@pytest_asyncio.fixture
async def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[Any]:
    return create_session_factory(migrated_engine)


@pytest_asyncio.fixture
async def repository(session_factory: async_sessionmaker[Any]) -> IndexingRepository:
    return IndexingRepository(session_factory)


@pytest_asyncio.fixture
async def repo_id(session_factory: async_sessionmaker[Any]) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        org = Org(name="acme", slug=f"acme-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        repo = Repo(org_id=org.id, name="widgets", full_name=f"acme/widgets-{uuid.uuid4().hex}")
        session.add(repo)
        await session.flush()
        return repo.id


async def _chunk_rows(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> list[IndexChunkRow]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(IndexChunkRow)
                    .where(IndexChunkRow.repo_id == repo_id)
                    .order_by(IndexChunkRow.file_path, IndexChunkRow.chunk_index)
                )
            )
            .scalars()
            .all()
        )


async def _node_rows(
    session_factory: async_sessionmaker[Any], repo_id: uuid.UUID
) -> list[IndexGraphNodeRow]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(IndexGraphNodeRow).where(IndexGraphNodeRow.repo_id == repo_id)
                )
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# US3/US4/US5: a real push job chunks, embeds, and graphs added/modified
# files end-to-end.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_job_chunks_embeds_and_graphs_added_files(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    tree_fetcher = _MultiCommitTreeFetcher(
        trees={_COMMIT_1: (_entry("caller.ts", _CALLER_TS), _entry("helper.ts", _HELPER_TS))},
        content={"caller.ts": _CALLER_TS, "helper.ts": _HELPER_TS},
    )
    deps = IndexingJobDependencies(
        tree_fetcher=tree_fetcher,
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )

    result = await run_indexing_job(
        _payload(repo_id=repo_id, added=["caller.ts", "helper.ts"]), deps, job_id="job-1"
    )

    assert result.files_processed == 2
    assert result.files_failed == 0
    assert result.used_full_tree_fallback is False

    chunks = await _chunk_rows(session_factory, repo_id)
    assert {row.file_path for row in chunks} == {"caller.ts", "helper.ts"}
    assert all(len(row.embedding) == 384 for row in chunks)

    nodes = await _node_rows(session_factory, repo_id)
    node_symbols = {row.symbol_name for row in nodes}
    assert "caller" in node_symbols
    assert "helper" in node_symbols

    async with session_factory() as session:
        edge_targets = (
            (await session.execute(select(IndexGraphEdgeRow.target_symbol_name))).scalars().all()
        )
    assert "helper" in edge_targets

    # US1: a canonical (pr_number == 0) push advances the cursor to the
    # pushed commit.
    assert await repository.get_last_indexed_commit(repo_id=repo_id) == _COMMIT_1


# ---------------------------------------------------------------------------
# US9: unsupported-language / ignored files never reach the chunker.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ignored_and_unsupported_language_files_never_reach_chunker(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    tree_fetcher = _MultiCommitTreeFetcher(
        trees={
            _COMMIT_1: (
                _entry("helper.go", b"package main\n\nfunc helper() {}\n"),
                _entry("README.md", b"# hello\n"),
                _entry("vendor/lib.go", b"package vendor\n"),
                _entry("go.sum", b"h1:abc\n"),
            )
        },
        content={
            "helper.go": b"package main\n\nfunc helper() {}\n",
            "README.md": b"# hello\n",
            "vendor/lib.go": b"package vendor\n",
            "go.sum": b"h1:abc\n",
        },
    )
    deps = IndexingJobDependencies(
        tree_fetcher=tree_fetcher,
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )

    result = await run_indexing_job(
        _payload(repo_id=repo_id, added=["helper.go", "README.md", "vendor/lib.go", "go.sum"]),
        deps,
        job_id="job-1",
    )

    assert result.files_processed == 1
    chunks = await _chunk_rows(session_factory, repo_id)
    assert {row.file_path for row in chunks} == {"helper.go"}


# ---------------------------------------------------------------------------
# US6/US7: incremental delta only touches the listed files; removed files
# are pruned via a real delete_file call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incremental_delta_leaves_untouched_files_alone(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("caller.ts", _CALLER_TS), _entry("helper.ts", _HELPER_TS))},
            content={"caller.ts": _CALLER_TS, "helper.ts": _HELPER_TS},
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )
    await run_indexing_job(
        _payload(repo_id=repo_id, added=["caller.ts", "helper.ts"]), deps, job_id="job-1"
    )

    # A later push only touches helper.ts — caller.ts must be left alone.
    updated_helper = b"function helper(): void {\n  console.log('v2');\n}\n"
    deps2 = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={
                _COMMIT_2: (
                    _entry("caller.ts", _CALLER_TS),
                    _entry("helper.ts", updated_helper),
                )
            },
            content={"helper.ts": updated_helper},
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )
    result = await run_indexing_job(
        _payload(repo_id=repo_id, commit_sha=_COMMIT_2, modified=["helper.ts"]),
        deps2,
        job_id="job-2",
    )

    assert result.files_processed == 1
    chunks = await _chunk_rows(session_factory, repo_id)
    helper_chunk = next(row for row in chunks if row.file_path == "helper.ts")
    assert "v2" in helper_chunk.content
    caller_chunk = next(row for row in chunks if row.file_path == "caller.ts")
    assert "v2" not in caller_chunk.content


@pytest.mark.asyncio
async def test_removed_file_is_deleted_via_delete_file(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("helper.ts", _HELPER_TS),)}, content={"helper.ts": _HELPER_TS}
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )
    await run_indexing_job(_payload(repo_id=repo_id, added=["helper.ts"]), deps, job_id="job-1")
    assert len(await _chunk_rows(session_factory, repo_id)) == 1

    deps2 = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(trees={_COMMIT_2: ()}),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )
    result = await run_indexing_job(
        _payload(repo_id=repo_id, commit_sha=_COMMIT_2, removed=["helper.ts"]),
        deps2,
        job_id="job-2",
    )

    assert result.files_processed == 1
    assert await _chunk_rows(session_factory, repo_id) == []
    assert await _node_rows(session_factory, repo_id) == []


@pytest.mark.asyncio
async def test_path_turned_symlink_deletes_its_stale_regular_file_rows(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    # A path indexed as a regular file...
    deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("helper.ts", _HELPER_TS),)}, content={"helper.ts": _HELPER_TS}
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )
    await run_indexing_job(_payload(repo_id=repo_id, added=["helper.ts"]), deps, job_id="job-1")
    assert len(await _chunk_rows(session_factory, repo_id)) == 1

    # ...later becomes a symlink on the same path. The mode-guard skip must
    # not leave the old regular-file rows behind forever — no other loop
    # ever revisits this path once it's no longer a regular file.
    symlink_entry = RepoTreeEntry(path="helper.ts", mode="120000", blob_sha="sha-symlink", size=5)
    deps2 = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(trees={_COMMIT_2: (symlink_entry,)}),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )
    result = await run_indexing_job(
        _payload(repo_id=repo_id, commit_sha=_COMMIT_2, modified=["helper.ts"]),
        deps2,
        job_id="job-2",
    )

    assert result.files_skipped == 1
    assert await _chunk_rows(session_factory, repo_id) == []


@pytest.mark.asyncio
async def test_path_both_added_and_removed_in_same_push_survives_indexed(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    # A path can be deleted in one commit and re-added in a later commit of
    # the same push — `apps/webhook_api/webhook_routes.py`'s
    # `_merged_push_file_lists` merges each commit's own added/removed
    # lists independently and has no way to net that across commits
    # itself, so the payload can genuinely list the same path in both
    # `added` and `removed`. helper.ts still exists in the current tree
    # (this push's real HEAD state, the one source of truth), so it must
    # survive indexed rather than be wiped by the removed-file loop right
    # after the added/modified loop just wrote it.
    deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("helper.ts", _HELPER_TS),)},
            content={"helper.ts": _HELPER_TS},
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )
    result = await run_indexing_job(
        _payload(repo_id=repo_id, added=["helper.ts"], removed=["helper.ts"]),
        deps,
        job_id="job-1",
    )

    assert result.files_processed == 1
    assert result.files_skipped == 0
    chunks = await _chunk_rows(session_factory, repo_id)
    assert [row.file_path for row in chunks] == ["helper.ts"]


@pytest.mark.asyncio
async def test_path_added_then_removed_in_same_push_counted_once_not_twice(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    """The mirror image of
    `test_path_both_added_and_removed_in_same_push_survives_indexed`:
    "ghost.ts" is genuinely gone from the current tree (unlike
    "helper.ts" above), so it must be reconciled out of
    `added_or_modified` the same way `removed_paths` already reconciles
    out a still-existing path — otherwise the added/modified loop's own
    `tree_by_path.get(path) is None` branch counts it as `files_skipped`
    *and* the removed-file loop separately counts it as `files_processed`,
    double-counting one file across two outcome buckets.
    """
    deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(trees={_COMMIT_1: ()}),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )
    result = await run_indexing_job(
        _payload(repo_id=repo_id, added=["ghost.ts"], removed=["ghost.ts"]),
        deps,
        job_id="job-1",
    )

    assert result.files_processed == 1
    assert result.files_skipped == 0
    assert result.files_failed == 0


# ---------------------------------------------------------------------------
# US8: truncated/force-push payloads fall back to a full-tree diff.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_tree_fallback_diffs_against_last_indexed_tree(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    # First, an ordinary push indexes a.ts at commit 1.
    content_a1 = b"function a(): void {}\n"
    deps1 = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("a.ts", content_a1, blob_sha="sha-a-1"),)},
            content={"a.ts": content_a1},
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )
    await run_indexing_job(_payload(repo_id=repo_id, added=["a.ts"]), deps1, job_id="job-1")

    # A force-pushed commit 2: a.ts modified (new blob sha), b.ts added,
    # nothing removed — force_full_tree_fallback=True means the payload's
    # own (empty) added/removed/modified lists must be ignored in favor of
    # a real tree diff against commit 1.
    content_a2 = b"function a(): void {\n  console.log('v2');\n}\n"
    content_b = b"function b(): void {}\n"
    multi_tree_fetcher = _MultiCommitTreeFetcher(
        trees={
            _COMMIT_1: (_entry("a.ts", content_a1, blob_sha="sha-a-1"),),
            _COMMIT_2: (
                _entry("a.ts", content_a2, blob_sha="sha-a-2"),
                _entry("b.ts", content_b, blob_sha="sha-b-1"),
            ),
        },
        content={"a.ts": content_a2, "b.ts": content_b},
    )
    deps2 = IndexingJobDependencies(
        tree_fetcher=multi_tree_fetcher,
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )
    result = await run_indexing_job(
        _payload(repo_id=repo_id, commit_sha=_COMMIT_2, force_full_tree_fallback=True),
        deps2,
        job_id="job-2",
    )

    assert result.used_full_tree_fallback is True
    assert result.files_processed == 2
    assert sorted(multi_tree_fetcher.fetch_tree_calls) == sorted([_COMMIT_2, _COMMIT_1])

    chunks = await _chunk_rows(session_factory, repo_id)
    assert {row.file_path for row in chunks} == {"a.ts", "b.ts"}
    a_chunk = next(row for row in chunks if row.file_path == "a.ts")
    assert "v2" in a_chunk.content
    assert await repository.get_last_indexed_commit(repo_id=repo_id) == _COMMIT_2


@pytest.mark.asyncio
async def test_full_tree_fallback_with_no_prior_index_treats_every_file_as_added(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("a.ts", _HELPER_TS), _entry("b.ts", _CALLER_TS))},
            content={"a.ts": _HELPER_TS, "b.ts": _CALLER_TS},
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )
    assert await repository.get_last_indexed_commit(repo_id=repo_id) is None

    result = await run_indexing_job(
        _payload(repo_id=repo_id, force_full_tree_fallback=True), deps, job_id="job-1"
    )

    assert result.files_processed == 2
    chunks = await _chunk_rows(session_factory, repo_id)
    assert {row.file_path for row in chunks} == {"a.ts", "b.ts"}


@pytest.mark.asyncio
async def test_truncated_tree_response_is_job_fatal_and_leaves_cursor_untouched(
    repository: IndexingRepository, repo_id: uuid.UUID
) -> None:
    await repository.set_last_indexed_commit(repo_id=repo_id, commit_sha=_COMMIT_1)

    deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(trees={}, truncated_for=frozenset({_COMMIT_2})),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )

    with pytest.raises(IndexingJobFailedError):
        await run_indexing_job(
            _payload(repo_id=repo_id, commit_sha=_COMMIT_2, added=["a.ts"]), deps, job_id="job-2"
        )

    assert await repository.get_last_indexed_commit(repo_id=repo_id) == _COMMIT_1


@pytest.mark.asyncio
async def test_unresolvable_previous_commit_in_full_tree_fallback_is_job_fatal(
    repository: IndexingRepository, repo_id: uuid.UUID
) -> None:
    # US8's full-tree-diff fallback fetches the previously-indexed
    # commit's own tree to diff against — if that commit is no longer
    # resolvable on GitHub (a force-push/history rewrite), the failure
    # must be job-fatal and visible (`IndexingJobFailedError`), never left
    # to propagate as an uncaught "genuine bug" that leaves the job stuck
    # `CLAIMED` forever with the cursor never advanced.
    await repository.set_last_indexed_commit(repo_id=repo_id, commit_sha=_COMMIT_1)

    deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_2: ()}, unreachable_for=frozenset({_COMMIT_1})
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )

    with pytest.raises(IndexingJobFailedError):
        await run_indexing_job(
            _payload(
                repo_id=repo_id,
                commit_sha=_COMMIT_2,
                pr_number=42,
                force_full_tree_fallback=True,
            ),
            deps,
            job_id="job-2",
        )

    assert await repository.get_last_indexed_commit(repo_id=repo_id) == _COMMIT_1


@pytest.mark.asyncio
async def test_batch_content_fetch_failure_fails_every_file_but_not_the_job(
    repository: IndexingRepository, repo_id: uuid.UUID
) -> None:
    # `fetch_content` fails its whole batch rather than a silently partial
    # result (e.g. one oversized blob among many) — this must be counted
    # as a per-file *failure* for every requested path, loudly, but must
    # not abort the job or leave the cursor stuck at its prior value: the
    # next push's delta must not keep re-including the same file forever.
    deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("a.ts", _HELPER_TS), _entry("b.ts", _CALLER_TS))},
            fail_content_fetch=True,
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )

    result = await run_indexing_job(
        _payload(repo_id=repo_id, added=["a.ts", "b.ts"]), deps, job_id="job-1"
    )

    assert result.files_processed == 0
    assert result.files_failed == 2
    assert result.files_skipped == 0
    assert await repository.get_last_indexed_commit(repo_id=repo_id) == _COMMIT_1


# ---------------------------------------------------------------------------
# US10: per-file, per-stage isolation — chunk, graph, embed, upsert, delete.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_files_chunk_parse_failure_does_not_abort_the_job(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    bad_content = b"\xff\xfe\x00\x01not valid utf-8"
    deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("good.ts", _HELPER_TS), _entry("bad.ts", bad_content))},
            content={"good.ts": _HELPER_TS, "bad.ts": bad_content},
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )

    result = await run_indexing_job(
        _payload(repo_id=repo_id, added=["good.ts", "bad.ts"]), deps, job_id="job-1"
    )

    assert result.files_processed == 1
    assert result.files_failed == 1
    chunks = await _chunk_rows(session_factory, repo_id)
    assert {row.file_path for row in chunks} == {"good.ts"}


@pytest.mark.asyncio
async def test_one_files_graph_extraction_failure_does_not_abort_the_job(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from indexing.graph import extract_graph as real_extract_graph

    def _flaky_extract_graph(language: Any, path: str, tree: Any) -> GraphResult:
        if path == "bad.ts":
            raise GraphExtractionError(file_path=path, language=str(language), detail="forced")
        return real_extract_graph(language, path, tree)

    monkeypatch.setattr(pipeline_module, "extract_graph", _flaky_extract_graph)

    deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("good.ts", _HELPER_TS), _entry("bad.ts", _CALLER_TS))},
            content={"good.ts": _HELPER_TS, "bad.ts": _CALLER_TS},
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )

    result = await run_indexing_job(
        _payload(repo_id=repo_id, added=["good.ts", "bad.ts"]), deps, job_id="job-1"
    )

    assert result.files_processed == 1
    assert result.files_failed == 1
    chunks = await _chunk_rows(session_factory, repo_id)
    # bad.ts's chunk stage never even runs its own upsert (the whole file's
    # remaining stages are skipped once graph fails) — no chunk row for it.
    assert {row.file_path for row in chunks} == {"good.ts"}


@pytest.mark.asyncio
async def test_one_files_embedding_failure_does_not_abort_the_job(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("good.ts", _HELPER_TS), _entry("bad.ts", _CALLER_TS))},
            content={"good.ts": _HELPER_TS, "bad.ts": _CALLER_TS},
        ),
        embedding_client=FakeEmbeddingClient(fail_on=_chunk_texts("bad.ts", _CALLER_TS)),
        index_repository=repository,
        commit_tracker=repository,
    )

    result = await run_indexing_job(
        _payload(repo_id=repo_id, added=["good.ts", "bad.ts"]), deps, job_id="job-1"
    )

    assert result.files_processed == 1
    assert result.files_failed == 1
    chunks = await _chunk_rows(session_factory, repo_id)
    assert {row.file_path for row in chunks} == {"good.ts"}


@dataclass
class _UpsertFailingOnPath:
    """Wraps a real `IndexingRepository`, forwarding every call except
    `upsert_file`, which raises `IndexingRepositoryError` for one
    specific file path — proves the "upsert" stage's own isolation without
    a real Postgres-level constraint violation."""

    real: IndexingRepository
    fail_path: str

    async def upsert_file(self, *, chunks: Sequence[Chunk], **kwargs: Any) -> None:
        if any(chunk.file_path == self.fail_path for chunk in chunks):
            raise IndexingRepositoryError(operation="upsert_file", detail="forced failure")
        await self.real.upsert_file(chunks=chunks, **kwargs)

    async def delete_file(self, **kwargs: Any) -> None:
        await self.real.delete_file(**kwargs)

    async def get_last_indexed_commit(self, **kwargs: Any) -> str | None:
        return await self.real.get_last_indexed_commit(**kwargs)

    async def set_last_indexed_commit(self, **kwargs: Any) -> None:
        await self.real.set_last_indexed_commit(**kwargs)


@pytest.mark.asyncio
async def test_one_files_upsert_failure_does_not_abort_the_job(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    wrapped = _UpsertFailingOnPath(real=repository, fail_path="bad.ts")
    deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("good.ts", _HELPER_TS), _entry("bad.ts", _CALLER_TS))},
            content={"good.ts": _HELPER_TS, "bad.ts": _CALLER_TS},
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=wrapped,
        commit_tracker=wrapped,
    )

    result = await run_indexing_job(
        _payload(repo_id=repo_id, added=["good.ts", "bad.ts"]), deps, job_id="job-1"
    )

    assert result.files_processed == 1
    assert result.files_failed == 1
    chunks = await _chunk_rows(session_factory, repo_id)
    assert {row.file_path for row in chunks} == {"good.ts"}


@dataclass
class _DeleteFailingOnPath:
    """Wraps a real `IndexingRepository`, forwarding every call except
    `delete_file`, which raises for one specific path — proves the
    "delete" stage's own isolation, distinct from "upsert"."""

    real: IndexingRepository
    fail_path: str

    async def upsert_file(self, **kwargs: Any) -> None:
        await self.real.upsert_file(**kwargs)

    async def delete_file(self, *, file_path: str, **kwargs: Any) -> None:
        if file_path == self.fail_path:
            raise IndexingRepositoryError(operation="delete_file", detail="forced failure")
        await self.real.delete_file(file_path=file_path, **kwargs)

    async def get_last_indexed_commit(self, **kwargs: Any) -> str | None:
        return await self.real.get_last_indexed_commit(**kwargs)

    async def set_last_indexed_commit(self, **kwargs: Any) -> None:
        await self.real.set_last_indexed_commit(**kwargs)


@pytest.mark.asyncio
async def test_one_files_delete_failure_does_not_abort_the_job(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    deps1 = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("good.ts", _HELPER_TS), _entry("bad.ts", _CALLER_TS))},
            content={"good.ts": _HELPER_TS, "bad.ts": _CALLER_TS},
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )
    await run_indexing_job(
        _payload(repo_id=repo_id, added=["good.ts", "bad.ts"]), deps1, job_id="job-1"
    )

    wrapped = _DeleteFailingOnPath(real=repository, fail_path="bad.ts")
    deps2 = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(trees={_COMMIT_2: ()}),
        embedding_client=FakeEmbeddingClient(),
        index_repository=wrapped,
        commit_tracker=wrapped,
    )
    result = await run_indexing_job(
        _payload(repo_id=repo_id, commit_sha=_COMMIT_2, removed=["good.ts", "bad.ts"]),
        deps2,
        job_id="job-2",
    )

    assert result.files_processed == 1
    assert result.files_failed == 1
    chunks = await _chunk_rows(session_factory, repo_id)
    # good.ts's delete succeeded; bad.ts's delete failed and its rows
    # survive.
    assert {row.file_path for row in chunks} == {"bad.ts"}


# ---------------------------------------------------------------------------
# pr_number == 0-only commit-advance rule.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_request_triggered_job_never_advances_last_indexed_commit(
    repository: IndexingRepository, repo_id: uuid.UUID, session_factory: async_sessionmaker[Any]
) -> None:
    assert await repository.get_last_indexed_commit(repo_id=repo_id) is None

    deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("a.ts", _HELPER_TS),)}, content={"a.ts": _HELPER_TS}
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )

    result = await run_indexing_job(
        _payload(repo_id=repo_id, pr_number=7, added=["a.ts"]), deps, job_id="job-1"
    )

    assert result.files_processed == 1
    # The cursor is untouched by a pull_request-triggered job...
    assert await repository.get_last_indexed_commit(repo_id=repo_id) is None
    # ...but the PR-scoped row was still written, per US2.
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(IndexChunkRow).where(
                        IndexChunkRow.repo_id == repo_id, IndexChunkRow.pr_number == 7
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# files_failed-ratio warning-log threshold.
# ---------------------------------------------------------------------------


def _named_ts(name: str) -> bytes:
    return f"function {name}(): void {{}}\n".encode()


def _chunk_texts(path: str, content: bytes) -> frozenset[str]:
    """The exact chunk text(s) `chunk_file` would produce for `content` —
    used to build a `FakeEmbeddingClient(fail_on=...)` set that actually
    matches (Tree-sitter's own node span excludes the file's trailing
    newline, so the raw `content` bytes themselves never equal any real
    chunk's own text)."""
    return frozenset(chunk.content for chunk in chunk_file("typescript", path, content).chunks)


def _reenable_pipeline_logger() -> None:
    """`adapter/storage`'s Alembic `env.py` calls
    `logging.config.fileConfig()` on every `migrated_engine` fixture setup
    (default `disable_existing_loggers=True`), which disables every
    already-created logger not explicitly listed in `alembic.ini` —
    including this module's own `indexing.pipeline` logger. Called at the
    top of every test below that asserts on `caplog`, mirroring
    `apps/worker/test_dispatch.py`'s own identical fix for
    `apps.worker.dispatch`."""
    logging.getLogger("indexing.pipeline").disabled = False


@pytest.mark.asyncio
async def test_failure_ratio_above_threshold_logs_at_warning(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _reenable_pipeline_logger()

    # 1 failing out of 4 total = 0.25 > 0.2 threshold.
    good_paths = [f"good{i}.ts" for i in range(3)]
    bad_path = "bad.ts"
    contents = {path: _named_ts(path.removesuffix(".ts")) for path in [*good_paths, bad_path]}
    tree_fetcher = _MultiCommitTreeFetcher(
        trees={_COMMIT_1: tuple(_entry(path, content) for path, content in contents.items())},
        content=contents,
    )
    deps = IndexingJobDependencies(
        tree_fetcher=tree_fetcher,
        embedding_client=FakeEmbeddingClient(fail_on=_chunk_texts(bad_path, contents[bad_path])),
        index_repository=repository,
        commit_tracker=repository,
    )

    with caplog.at_level("INFO", logger="indexing.pipeline"):
        result = await run_indexing_job(
            _payload(repo_id=repo_id, added=[*good_paths, bad_path]), deps, job_id="job-1"
        )

    assert result.files_processed == 3
    assert result.files_failed == 1
    completed = [r for r in caplog.records if r.getMessage() == "index job completed"]
    assert len(completed) == 1
    assert completed[0].levelname == "WARNING"


@pytest.mark.asyncio
async def test_failure_ratio_at_or_below_threshold_logs_at_info(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _reenable_pipeline_logger()

    # 1 failing / 5 processed = 0.2 exactly — the pipeline's own ratio is
    # files_failed / files_processed, not files_failed / total attempted —
    # not > 0.2, so this stays at the info level (the boundary case).
    good_paths = [f"good{i}.ts" for i in range(5)]
    bad_path = "bad.ts"
    contents = {path: _named_ts(path.removesuffix(".ts")) for path in [*good_paths, bad_path]}
    tree_fetcher = _MultiCommitTreeFetcher(
        trees={_COMMIT_1: tuple(_entry(path, content) for path, content in contents.items())},
        content=contents,
    )
    deps = IndexingJobDependencies(
        tree_fetcher=tree_fetcher,
        embedding_client=FakeEmbeddingClient(fail_on=_chunk_texts(bad_path, contents[bad_path])),
        index_repository=repository,
        commit_tracker=repository,
    )

    with caplog.at_level("INFO", logger="indexing.pipeline"):
        result = await run_indexing_job(
            _payload(repo_id=repo_id, added=[*good_paths, bad_path]), deps, job_id="job-1"
        )

    assert result.files_processed == 5
    assert result.files_failed == 1
    completed = [r for r in caplog.records if r.getMessage() == "index job completed"]
    assert len(completed) == 1
    assert completed[0].levelname == "INFO"


# ---------------------------------------------------------------------------
# Cost ceilings (US6/US8's NFR).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialization_ceiling_drops_excess_files(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _reenable_pipeline_logger()
    monkeypatch.setattr(pipeline_module, "INCREMENTAL_FILE_CEILING", 3)

    paths = [f"f{i}.ts" for i in range(4)]
    contents = {path: _named_ts(path.removesuffix(".ts")) for path in paths}
    tree_fetcher = _MultiCommitTreeFetcher(
        trees={_COMMIT_1: tuple(_entry(path, content) for path, content in contents.items())},
        content=contents,
    )
    deps = IndexingJobDependencies(
        tree_fetcher=tree_fetcher,
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )

    with caplog.at_level("WARNING", logger="indexing.pipeline"):
        result = await run_indexing_job(
            _payload(repo_id=repo_id, added=paths), deps, job_id="job-1"
        )

    assert result.files_processed == 3
    assert result.files_skipped == 1
    chunks = await _chunk_rows(session_factory, repo_id)
    assert len({row.file_path for row in chunks}) == 3
    assert any(
        "materialization file-count ceiling exceeded" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_aggregate_bytes_ceiling_drops_excess_files(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _reenable_pipeline_logger()
    monkeypatch.setattr(pipeline_module, "INCREMENTAL_BYTES_CEILING", 100)

    small = _entry("small.ts", _named_ts("small"))
    # A tree entry whose declared `size` alone busts the tiny ceiling above
    # — the pipeline uses the tree entry's own `size` field for the
    # ceiling calculation, not the real fetched content length.
    big_content = _named_ts("big")
    big = RepoTreeEntry(path="big.ts", mode="100644", blob_sha="sha-big", size=1_000)
    contents = {"small.ts": _named_ts("small"), "big.ts": big_content}
    tree_fetcher = _MultiCommitTreeFetcher(trees={_COMMIT_1: (small, big)}, content=contents)
    deps = IndexingJobDependencies(
        tree_fetcher=tree_fetcher,
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )

    with caplog.at_level("WARNING", logger="indexing.pipeline"):
        result = await run_indexing_job(
            _payload(repo_id=repo_id, added=["small.ts", "big.ts"]), deps, job_id="job-1"
        )

    assert result.files_processed == 1
    assert result.files_skipped == 1
    chunks = await _chunk_rows(session_factory, repo_id)
    assert {row.file_path for row in chunks} == {"small.ts"}
    assert any("aggregate-bytes ceiling exceeded" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_path_absent_from_current_tree_is_skipped_exactly_once(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
) -> None:
    """A path present in the job's added/modified list but absent from the
    current commit's tree — e.g. a single push whose merged commits add a
    file and then remove it again, so the merged webhook payload's `added`
    list still names it even though the final pushed HEAD's tree does not
    — must be counted in `files_skipped` exactly once.

    Regression test: `entries` (built by filtering `added_or_modified` down
    to paths present in `tree_by_path`) used to add its own
    `len(added_or_modified) - len(entries)` to `files_skipped`, and then the
    per-file loop below independently re-checked `tree_by_path.get(path) is
    None` for the very same paths and added to `files_skipped` again —
    double-counting every tree-lookup miss.
    """
    tree_fetcher = _MultiCommitTreeFetcher(
        trees={_COMMIT_1: (_entry("real.ts", _HELPER_TS),)},
        content={"real.ts": _HELPER_TS},
    )
    deps = IndexingJobDependencies(
        tree_fetcher=tree_fetcher,
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )

    result = await run_indexing_job(
        _payload(repo_id=repo_id, added=["ghost.ts", "real.ts"]), deps, job_id="job-1"
    )

    assert result.files_processed == 1
    assert result.files_skipped == 1


# ---------------------------------------------------------------------------
# Whole-job Postgres-failure/resume case.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whole_job_postgres_failure_leaves_cursor_untouched_and_retry_reprocesses(
    repository: IndexingRepository,
    repo_id: uuid.UUID,
    session_factory: async_sessionmaker[Any],
) -> None:
    broken_engine = create_storage_engine("postgresql+asyncpg://user:pass@127.0.0.1:1/nonexistent")
    broken_repository = IndexingRepository(create_session_factory(broken_engine))
    try:
        tree_fetcher = _MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("a.ts", _HELPER_TS),)}, content={"a.ts": _HELPER_TS}
        )
        broken_deps = IndexingJobDependencies(
            tree_fetcher=tree_fetcher,
            embedding_client=FakeEmbeddingClient(),
            index_repository=broken_repository,
            commit_tracker=broken_repository,
        )

        with pytest.raises(IndexingJobFailedError):
            await run_indexing_job(
                _payload(repo_id=repo_id, added=["a.ts"]), broken_deps, job_id="job-1"
            )
    finally:
        await broken_engine.dispose()

    # The cursor stays at its prior (never-set) value — no partial/garbled
    # state from the aborted job.
    assert await repository.get_last_indexed_commit(repo_id=repo_id) is None
    assert await _chunk_rows(session_factory, repo_id) == []

    # A retried job against a healthy repository reprocesses cleanly.
    healthy_deps = IndexingJobDependencies(
        tree_fetcher=_MultiCommitTreeFetcher(
            trees={_COMMIT_1: (_entry("a.ts", _HELPER_TS),)}, content={"a.ts": _HELPER_TS}
        ),
        embedding_client=FakeEmbeddingClient(),
        index_repository=repository,
        commit_tracker=repository,
    )
    result = await run_indexing_job(
        _payload(repo_id=repo_id, added=["a.ts"]), healthy_deps, job_id="job-2"
    )

    assert result.files_processed == 1
    assert await repository.get_last_indexed_commit(repo_id=repo_id) == _COMMIT_1
    assert len(await _chunk_rows(session_factory, repo_id)) == 1
