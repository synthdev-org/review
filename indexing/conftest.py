"""Shared test fakes for the ``indexing`` package, per
``docs/rules/testing.md``'s "fakes over mocks for adapters" convention.

Per the indexing delivery plan's Assumption 8 (corrected during that
plan's own reuse review — see ``FakeRepoTreeFetcher``'s own docstring
below): ``indexing/conftest.py`` holds fakes needed by *multiple* test
files across later phases (``FakeRepoTreeFetcher`` here, Phase 2;
``FakeEmbeddingClient``, Phase 4) — a deliberately different, shared
pattern for this package, unlike ``review``'s one-off, per-test-file
``_FakeXxx`` classes (e.g. ``_FakeDiffFetcher``, independently redefined
across several ``review/test_*.py`` files).

Per ``docs/rules/testing.md``'s middle tier: ``indexing/test_pipeline.py``
(Phase 7) is this package's one DB-touching integration test —
``migrated_engine``/``pgvector_container`` are imported from
``adapter/storage/conftest.py`` (not redefined here), the same reuse
pattern ``apps/webhook_api/conftest.py`` already established.

``reset_otel_global_providers`` is imported from
``adapter/telemetry/conftest.py`` for the same reason
``review/conftest.py`` already imports it — US9's
``indexing/test_pipeline_telemetry.py`` needs it to register a real,
in-memory-backed ``TracerProvider``/``MeterProvider`` per test, and a
fixture is only discoverable by pytest across this whole package if it's
imported here, not redefined per test file.
"""

from __future__ import annotations

from collections.abc import Sequence

from adapter.storage.conftest import migrated_engine, pgvector_container  # noqa: F401
from adapter.telemetry.conftest import reset_otel_global_providers  # noqa: F401
from indexing.embedding import EmbeddingClient, EmbeddingError, EmbeddingVector
from indexing.tree import RepoTreeEntry, RepoTreeFetcher, RepoTreeTruncatedError


class FakeRepoTreeFetcher:
    """A deterministic, in-memory ``indexing.tree.RepoTreeFetcher``
    implementation — used by Phase 6/7's pipeline-level tests instead of a
    recorded GitHub cassette, per the plan's Phase 2 Tests section.

    Construct with the ``tree`` this fake should return from ``fetch_tree``
    and the ``content`` dict ``fetch_content`` should draw from (keyed by
    path). Set ``truncated=True`` to make ``fetch_tree`` raise
    ``RepoTreeTruncatedError`` instead, exercising US8's full-tree-fallback
    failure path without a real GitHub API cassette. Every call is recorded
    (``fetch_tree_calls``/``fetch_content_calls``) so a consuming test can
    assert exactly what a caller asked for, without a mock's brittle
    call-count/argument-matcher machinery.
    """

    def __init__(
        self,
        *,
        tree: tuple[RepoTreeEntry, ...] = (),
        content: dict[str, bytes] | None = None,
        truncated: bool = False,
    ) -> None:
        self._tree = tree
        self._content = dict(content) if content is not None else {}
        self._truncated = truncated
        self.fetch_tree_calls: list[tuple[int, str, str]] = []
        self.fetch_content_calls: list[tuple[int, str, tuple[str, ...]]] = []

    async def fetch_tree(
        self, *, installation_id: int, repo_full_name: str, commit_sha: str
    ) -> tuple[RepoTreeEntry, ...]:
        self.fetch_tree_calls.append((installation_id, repo_full_name, commit_sha))
        if self._truncated:
            raise RepoTreeTruncatedError(repo_full_name=repo_full_name, commit_sha=commit_sha)
        return self._tree

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        paths_with_blob_sha: Sequence[RepoTreeEntry],
    ) -> dict[str, bytes]:
        paths = tuple(entry.path for entry in paths_with_blob_sha)
        self.fetch_content_calls.append((installation_id, repo_full_name, paths))
        return {path: self._content[path] for path in paths if path in self._content}


# Runtime confirmation (not just a docstring claim) that this fake actually
# satisfies the Protocol it stands in for, mirroring
# adapter/github/tree.py's own runtime check.
_: type[RepoTreeFetcher] = FakeRepoTreeFetcher


class FakeEmbeddingClient:
    """A deterministic, fixed-vector ``indexing.embedding.EmbeddingClient``
    implementation, per the plan's Phase 4 Tests section — used by Phase
    6/7's pipeline-level tests instead of the real local ONNX model, per
    ``testing.md``'s "fakes over mocks for adapters" convention.

    Every text embeds to the same ``vector`` (default: a fixed, arbitrary
    384-dimension unit-scale vector, matching the real model's own
    dimension — see ``adapter/embedding/local_onnx.py``'s
    ``EMBEDDING_DIMENSION``) unless ``fail_on`` names specific texts that
    should raise ``EmbeddingError`` instead, for exercising Phase 7's
    per-file isolation (US10) without a real corrupted model artifact.
    Every call is recorded (``embed_calls``), the same
    call-recording-over-mock-assertions shape ``FakeRepoTreeFetcher`` above
    already establishes.
    """

    def __init__(
        self,
        *,
        vector: tuple[float, ...] | None = None,
        fail_on: frozenset[str] = frozenset(),
    ) -> None:
        self._vector = vector if vector is not None else tuple([0.1] * 384)
        self._fail_on = fail_on
        self.embed_calls: list[tuple[str, ...]] = []

    def embed(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        texts_tuple = tuple(texts)
        self.embed_calls.append(texts_tuple)

        for text in texts_tuple:
            if text in self._fail_on:
                raise EmbeddingError(detail=f"forced failure for text: {text!r}")

        return tuple(
            EmbeddingVector(values=self._vector, dimension=len(self._vector)) for _ in texts_tuple
        )


# Runtime confirmation (not just a docstring claim) that this fake actually
# satisfies the Protocol it stands in for, mirroring
# adapter/github/tree.py's own runtime check.
__: type[EmbeddingClient] = FakeEmbeddingClient
