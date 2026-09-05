"""Tests for adapter.github.tree — the concrete RepoTreeFetcher
implementation, per the indexing delivery plan's Phase 2 section and
docs/rules/testing.md's "GitHub adapter against a recorded cassette (respx
for httpx)" convention.
"""

from __future__ import annotations

import base64
import logging
import re

import httpx
import pytest
import respx

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.tree import GitHubTreeFetcher
from indexing.tree import (
    RepoTreeContentFetchError,
    RepoTreeEntry,
    RepoTreeFetchError,
    RepoTreeTruncatedError,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OWNER_REPO = "acme/widgets"
_INSTALLATION_ID = 555
_COMMIT_SHA = "b" * 40
_BASE = "https://api.github.com"


@pytest.fixture(autouse=True)
def _reenable_tree_loggers() -> None:
    """See adapter/github/test_diff.py's own identical fixture: Alembic's
    `env.py` can disable already-created loggers not listed in
    `alembic.ini` elsewhere in the same `pytest -m integration` process.
    Re-enabled here, autouse, so every `caplog`-asserting case below stays
    deterministic regardless of test order.
    """
    logging.getLogger("adapter.github.tree").disabled = False
    logging.getLogger("adapter.github.git_data").disabled = False


def _mock_token_exchange() -> None:
    respx.post(f"{_BASE}/app/installations/{_INSTALLATION_ID}/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_test_token", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )


def _fetcher(
    test_rsa_private_key_pem: str,
    *,
    max_retry_attempts: int = 3,
    blob_fetch_concurrency: int = 10,
) -> GitHubTreeFetcher:
    provider = GitHubInstallationTokenProvider(app_id=1, private_key_pem=test_rsa_private_key_pem)
    return GitHubTreeFetcher(
        token_provider=provider,
        max_retry_attempts=max_retry_attempts,
        blob_fetch_concurrency=blob_fetch_concurrency,
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# fetch_tree(): full listing, directory exclusion, truncation
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_tree_returns_files_and_submodules_excluding_directories(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/git/trees/{_COMMIT_SHA}?recursive=1").mock(
        return_value=httpx.Response(
            200,
            json={
                "tree": [
                    {"path": "src", "mode": "040000", "type": "tree", "sha": "sha-dir"},
                    {
                        "path": "src/a.go",
                        "mode": "100644",
                        "type": "blob",
                        "sha": "sha-a",
                        "size": 123,
                    },
                    {
                        "path": "vendor/lib",
                        "mode": "160000",
                        "type": "commit",
                        "sha": "sha-submodule",
                    },
                    {
                        "path": "link.go",
                        "mode": "120000",
                        "type": "blob",
                        "sha": "sha-link",
                        "size": 7,
                    },
                ],
                "truncated": False,
            },
        )
    )

    tree = await _fetcher(test_rsa_private_key_pem).fetch_tree(
        installation_id=_INSTALLATION_ID, repo_full_name=_OWNER_REPO, commit_sha=_COMMIT_SHA
    )

    assert {entry.path for entry in tree} == {"src/a.go", "vendor/lib", "link.go"}
    by_path = {entry.path: entry for entry in tree}
    assert by_path["src/a.go"].blob_sha == "sha-a"
    assert by_path["src/a.go"].size == 123
    assert by_path["vendor/lib"].mode == "160000"
    assert by_path["vendor/lib"].size is None
    assert by_path["link.go"].mode == "120000"


@respx.mock
async def test_fetch_tree_truncated_raises_and_logs_error(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    _mock_token_exchange()
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/git/trees/{_COMMIT_SHA}?recursive=1").mock(
        return_value=httpx.Response(
            200,
            json={
                "tree": [{"path": "a.go", "mode": "100644", "type": "blob", "sha": "sha-a"}],
                "truncated": True,
            },
        )
    )

    with (
        caplog.at_level("ERROR", logger="adapter.github.tree"),
        pytest.raises(RepoTreeTruncatedError),
    ):
        await _fetcher(test_rsa_private_key_pem).fetch_tree(
            installation_id=_INSTALLATION_ID, repo_full_name=_OWNER_REPO, commit_sha=_COMMIT_SHA
        )

    assert any(r.levelname == "ERROR" for r in caplog.records)


@respx.mock
async def test_fetch_tree_unreachable_raises_typed_error(test_rsa_private_key_pem: str) -> None:
    _mock_token_exchange()
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/git/trees/{_COMMIT_SHA}?recursive=1").mock(
        side_effect=httpx.ConnectError("refused")
    )

    with pytest.raises(RepoTreeFetchError):
        await _fetcher(test_rsa_private_key_pem, max_retry_attempts=0).fetch_tree(
            installation_id=_INSTALLATION_ID, repo_full_name=_OWNER_REPO, commit_sha=_COMMIT_SHA
        )


# ---------------------------------------------------------------------------
# fetch_content(): arbitrary (non-diff-scoped) path set, symlink/submodule
# exclusion, fail-whole-call-on-partial-blob-failure
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_content_arbitrary_path_set_not_diff_scoped(
    test_rsa_private_key_pem: str,
) -> None:
    """`fetch_content` is not tied to any prior `fetch_tree` call for the
    same repo — an arbitrary set of `RepoTreeEntry` items (e.g. sourced
    from two different commits' listings) is accepted directly, unlike
    `adapter.github.diff.GitHubDiffFetcher.fetch_content`'s diff-scoped
    `paths` parameter.
    """
    _mock_token_exchange()
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/git/blobs/sha-a").mock(
        return_value=httpx.Response(200, json={"content": _b64(b"package a")})
    )
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/git/blobs/sha-b").mock(
        return_value=httpx.Response(200, json={"content": _b64(b"package b")})
    )

    entries = (
        RepoTreeEntry(path="a.go", mode="100644", blob_sha="sha-a", size=9),
        RepoTreeEntry(path="b.go", mode="100644", blob_sha="sha-b", size=9),
    )

    content = await _fetcher(test_rsa_private_key_pem).fetch_content(
        installation_id=_INSTALLATION_ID, repo_full_name=_OWNER_REPO, paths_with_blob_sha=entries
    )

    assert content == {"a.go": b"package a", "b.go": b"package b"}


@respx.mock
async def test_fetch_content_skips_symlink_and_submodule_entries(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    blob_route = respx.get(re.compile(rf"{re.escape(_BASE)}/repos/{_OWNER_REPO}/git/blobs/.+"))
    blob_route.mock(return_value=httpx.Response(200, json={"content": _b64(b"regular")}))

    entries = (
        RepoTreeEntry(path="link", mode="120000", blob_sha="sha-link", size=5),
        RepoTreeEntry(path="submod", mode="160000", blob_sha="sha-submod", size=None),
        RepoTreeEntry(path="regular.go", mode="100644", blob_sha="sha-regular", size=7),
    )

    content = await _fetcher(test_rsa_private_key_pem).fetch_content(
        installation_id=_INSTALLATION_ID, repo_full_name=_OWNER_REPO, paths_with_blob_sha=entries
    )

    assert content == {"regular.go": b"regular"}
    assert blob_route.call_count == 1


@respx.mock
async def test_fetch_content_blob_at_size_ceiling_is_fatal_and_never_fetched(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    _mock_token_exchange()
    blob_route = respx.get(re.compile(rf"{re.escape(_BASE)}/repos/{_OWNER_REPO}/git/blobs/.+"))
    blob_route.mock(return_value=httpx.Response(200, json={"content": _b64(b"x")}))

    entries = (
        RepoTreeEntry(path="huge.bin", mode="100644", blob_sha="sha-huge", size=100_000_000),
    )

    with (
        caplog.at_level("ERROR", logger="adapter.github.git_data"),
        pytest.raises(RepoTreeContentFetchError),
    ):
        await _fetcher(test_rsa_private_key_pem).fetch_content(
            installation_id=_INSTALLATION_ID,
            repo_full_name=_OWNER_REPO,
            paths_with_blob_sha=entries,
        )

    assert blob_route.call_count == 0
    assert any(r.levelname == "ERROR" for r in caplog.records)


@respx.mock
async def test_fetch_content_partial_blob_failure_fails_whole_call(
    test_rsa_private_key_pem: str,
) -> None:
    """One failing blob fetch (a permanent 404) fails the whole call —
    never a silently partial `dict[str, bytes]` with the failing path
    missing and the succeeding ones present.
    """
    _mock_token_exchange()
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/git/blobs/sha-ok").mock(
        return_value=httpx.Response(200, json={"content": _b64(b"fine")})
    )
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/git/blobs/sha-missing").mock(
        return_value=httpx.Response(404)
    )

    entries = (
        RepoTreeEntry(path="ok.go", mode="100644", blob_sha="sha-ok", size=4),
        RepoTreeEntry(path="missing.go", mode="100644", blob_sha="sha-missing", size=4),
    )

    with pytest.raises(RepoTreeContentFetchError):
        await _fetcher(test_rsa_private_key_pem).fetch_content(
            installation_id=_INSTALLATION_ID,
            repo_full_name=_OWNER_REPO,
            paths_with_blob_sha=entries,
        )
