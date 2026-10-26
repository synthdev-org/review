"""adapter.local.unavailable — stand-in for every server-side data service local mode lacks.

The shared registry builder takes a tree fetcher, graph/similarity/clone queries, an embedding
client and a memory matcher because the clone-detection, context-builder and memory-suppression
steps use them. Local mode keeps those steps disabled (plan A3: no database, no code index), so the
objects are never called; this one class satisfies all of those Protocols and fails loudly if a
configuration mistake ever enables one of the steps. It is also the "stub" the plan allows in place
of a ``LocalTreeFetcher``, since no enabled step needs a repository tree.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from adapter.local.exceptions import IndexUnavailableError
from indexing.chunk import Chunk
from indexing.clone import CloneFileIndex
from indexing.embedding import EmbeddingVector
from indexing.tree import RepoTreeEntry
from review.clone_detection import CloneCandidates
from review.context_builder import CallerHit


class UnavailableIndexServices:
    async def fetch_tree(
        self, *, installation_id: int, repo_full_name: str, commit_sha: str
    ) -> tuple[RepoTreeEntry, ...]:
        raise IndexUnavailableError(capability="repository tree")

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        paths_with_blob_sha: Sequence[RepoTreeEntry],
    ) -> dict[str, bytes]:
        raise IndexUnavailableError(capability="repository tree")

    async def find_callers(
        self, *, repo_id: str, symbol_names: Sequence[str], limit_per_symbol: int
    ) -> dict[str, tuple[CallerHit, ...]]:
        raise IndexUnavailableError(capability="call graph")

    async def find_similar(
        self, *, repo_id: str, embedding: EmbeddingVector, exclude_file_path: str, limit: int
    ) -> tuple[Chunk, ...]:
        raise IndexUnavailableError(capability="similarity search")

    async def find_candidates(
        self,
        *,
        repo_id: str,
        hashes: Sequence[int],
        excluded_paths: Sequence[str],
        per_hash_limit: int,
        timeout_seconds: float,
    ) -> CloneCandidates:
        raise IndexUnavailableError(capability="clone index")

    async def load_files(
        self, *, repo_id: str, file_paths: Sequence[str], timeout_seconds: float
    ) -> dict[str, CloneFileIndex]:
        raise IndexUnavailableError(capability="clone index")

    def embed(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        raise IndexUnavailableError(capability="embedding model")

    async def best_effective_similarity(
        self, *, repo_id: str, embedding: tuple[float, ...], half_life_days: float, now: datetime
    ) -> float | None:
        raise IndexUnavailableError(capability="memory store")


class EmptyIndexServices:
    """The same stand-in for a run that enables the context builder only for its MCP provider
    branch (ADR-0017): local mode still has no code index, so every code lookup answers "nothing
    found" and the code branch finishes empty (an info log), where ``UnavailableIndexServices``
    would raise because a step that needs the index should never have been enabled."""

    async def fetch_tree(
        self, *, installation_id: int, repo_full_name: str, commit_sha: str
    ) -> tuple[RepoTreeEntry, ...]:
        return ()

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        paths_with_blob_sha: Sequence[RepoTreeEntry],
    ) -> dict[str, bytes]:
        return {}

    async def find_callers(
        self, *, repo_id: str, symbol_names: Sequence[str], limit_per_symbol: int
    ) -> dict[str, tuple[CallerHit, ...]]:
        return {}

    async def find_similar(
        self, *, repo_id: str, embedding: EmbeddingVector, exclude_file_path: str, limit: int
    ) -> tuple[Chunk, ...]:
        return ()

    def embed(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        return tuple(EmbeddingVector(values=(), dimension=0) for _ in texts)
