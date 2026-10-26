"""Unit tests for ``adapter.local.unavailable``: the null-object index services for local mode."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adapter.local.exceptions import IndexUnavailableError
from adapter.local.unavailable import EmptyIndexServices, UnavailableIndexServices


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "capability"),
    [
        (
            lambda s: s.fetch_tree(installation_id=0, repo_full_name="a/b", commit_sha="x"),
            "repository tree",
        ),
        (
            lambda s: s.fetch_content(
                installation_id=0, repo_full_name="a/b", paths_with_blob_sha=[]
            ),
            "repository tree",
        ),
        (
            lambda s: s.find_callers(repo_id="r", symbol_names=["f"], limit_per_symbol=1),
            "call graph",
        ),
        (
            lambda s: s.find_similar(
                repo_id="r",
                embedding=None,
                exclude_file_path="a.py",
                limit=1,  # type: ignore[arg-type]
            ),
            "similarity search",
        ),
        (
            lambda s: s.find_candidates(
                repo_id="r", hashes=[1], excluded_paths=[], per_hash_limit=1, timeout_seconds=1.0
            ),
            "clone index",
        ),
        (
            lambda s: s.load_files(repo_id="r", file_paths=["a.py"], timeout_seconds=1.0),
            "clone index",
        ),
        (
            lambda s: s.best_effective_similarity(
                repo_id="r", embedding=(0.0,), half_life_days=1.0, now=datetime.now(UTC)
            ),
            "memory store",
        ),
    ],
)
async def test_every_async_lookup_fails_loudly_naming_the_missing_capability(
    call: object, capability: str
) -> None:
    with pytest.raises(IndexUnavailableError) as raised:
        await call(UnavailableIndexServices())  # type: ignore[operator]

    assert raised.value.capability == capability


def test_embedding_fails_loudly_too() -> None:
    with pytest.raises(IndexUnavailableError, match="embedding model"):
        UnavailableIndexServices().embed(["x"])


@pytest.mark.asyncio
async def test_the_empty_services_answer_nothing_found_for_every_code_lookup() -> None:
    empty = EmptyIndexServices()

    assert await empty.fetch_tree(installation_id=0, repo_full_name="a/b", commit_sha="x") == ()
    assert (
        await empty.fetch_content(installation_id=0, repo_full_name="a/b", paths_with_blob_sha=[])
        == {}
    )
    assert await empty.find_callers(repo_id="r", symbol_names=["f"], limit_per_symbol=1) == {}
    assert (
        await empty.find_similar(
            repo_id="r",
            embedding=None,
            exclude_file_path="a.py",
            limit=1,  # type: ignore[arg-type]
        )
        == ()
    )


def test_the_empty_services_embed_each_text_to_an_empty_vector() -> None:
    vectors = EmptyIndexServices().embed(["a", "b"])

    assert [v.dimension for v in vectors] == [0, 0]
