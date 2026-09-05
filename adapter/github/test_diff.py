"""Tests for adapter.github.diff — the concrete DiffFetcher implementation,
per the plan's Phase 4 section and docs/rules/testing.md's "GitHub adapter
against a recorded cassette (respx for httpx)" convention.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re

import httpx
import pytest
import respx

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.diff import GitHubDiffFetcher
from adapter.github.exceptions import (
    GitHubBlobTooLargeError,
    GitHubDiffFetchMalformedResponseError,
    GitHubDiffFetchUnreachableError,
    GitHubFileCountMismatchError,
    GitHubTreeEntryMissingError,
)
from ingestion.diff import Diff, DiffFile

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OWNER_REPO = "acme/widgets"
_INSTALLATION_ID = 555
_PR_NUMBER = 42
_HEAD_SHA = "a" * 40
_BASE = "https://api.github.com"


@pytest.fixture(autouse=True)
def _reenable_diff_logger() -> None:
    """Alembic's `env.py` (triggered by any `migrated_engine`-using test
    elsewhere in the same `pytest -m integration` process — this file's own
    tests never use it, but `pyproject.toml`'s `testpaths` ordering means
    one can easily have already run first) disables every already-created
    logger not listed in `alembic.ini`, including this module's own
    `adapter.github.diff` logger. Re-enabled here, autouse, so every
    `caplog`-asserting case below stays deterministic regardless of test
    order — mirroring `apps/webhook_api/test_webhook_routes.py`'s own
    per-test version of this same fix. `adapter.github.git_data` is
    included too (indexing delivery plan Phase 2) — `fetch_content`'s
    symlink/submodule/size-ceiling logging now lives in that shared module,
    not here, per the extraction that phase makes.
    """
    logging.getLogger("adapter.github.diff").disabled = False
    logging.getLogger("adapter.github.git_data").disabled = False


def _mock_token_exchange() -> None:
    respx.post(f"{_BASE}/app/installations/{_INSTALLATION_ID}/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_test_token", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )


def _mock_pr(changed_files: int) -> None:
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}").mock(
        return_value=httpx.Response(200, json={"changed_files": changed_files})
    )


def _fetcher(
    test_rsa_private_key_pem: str,
    *,
    max_retry_attempts: int = 3,
    blob_fetch_concurrency: int = 10,
) -> GitHubDiffFetcher:
    provider = GitHubInstallationTokenProvider(app_id=1, private_key_pem=test_rsa_private_key_pem)
    return GitHubDiffFetcher(
        token_provider=provider,
        max_retry_attempts=max_retry_attempts,
        blob_fetch_concurrency=blob_fetch_concurrency,
    )


async def _fetch(fetcher: GitHubDiffFetcher) -> Diff:
    return await fetcher.fetch(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        pr_number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
    )


# ---------------------------------------------------------------------------
# fetch(): pagination, count-mismatch guard, tree cross-reference
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_follows_and_merges_a_second_page_of_files(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    _mock_pr(changed_files=2)
    page_one_url = f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/files?per_page=100"
    page_two_url = f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/files?per_page=100&page=2"
    respx.get(page_one_url).mock(
        return_value=httpx.Response(
            200,
            json=[{"filename": "a.py", "status": "modified", "patch": "@@ ... @@"}],
            headers={"Link": f'<{page_two_url}>; rel="next"'},
        )
    )
    respx.get(page_two_url).mock(
        return_value=httpx.Response(
            200, json=[{"filename": "b.py", "status": "added", "patch": "@@ ... @@"}]
        )
    )
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/git/trees/{_HEAD_SHA}?recursive=1").mock(
        return_value=httpx.Response(
            200,
            json={
                "tree": [
                    {"path": "a.py", "mode": "100644", "sha": "sha-a", "size": 10},
                    {"path": "b.py", "mode": "100644", "sha": "sha-b", "size": 20},
                ],
                "truncated": False,
            },
        )
    )

    diff = await _fetch(_fetcher(test_rsa_private_key_pem))

    assert {f.path for f in diff.files} == {"a.py", "b.py"}


@respx.mock
async def test_fetch_file_count_mismatch_is_fatal_and_logs_error(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    _mock_token_exchange()
    _mock_pr(changed_files=5)  # disagrees with the single file the Files-endpoint returns
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/files?per_page=100").mock(
        return_value=httpx.Response(
            200, json=[{"filename": "a.py", "status": "modified", "patch": "@@ @@"}]
        )
    )
    # `fetch()` issues the PR/files/tree calls concurrently (`asyncio.gather`)
    # — the count-mismatch check below only runs once all three have
    # resolved, so the tree call must be mocked too, even though its body is
    # never consulted on this path (an unmocked call here races
    # non-deterministically against the other two, per respx's own
    # `AllMockedAssertionError` on an un-routed request).
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/git/trees/{_HEAD_SHA}?recursive=1").mock(
        return_value=httpx.Response(200, json={"tree": [], "truncated": False})
    )

    with (
        caplog.at_level("ERROR", logger="adapter.github.diff"),
        pytest.raises(GitHubFileCountMismatchError),
    ):
        await _fetch(_fetcher(test_rsa_private_key_pem))

    assert any(r.levelname == "ERROR" for r in caplog.records)


@respx.mock
async def test_fetch_truncated_tree_with_every_changed_path_resolved_is_accepted(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    _mock_pr(changed_files=1)
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/files?per_page=100").mock(
        return_value=httpx.Response(
            200, json=[{"filename": "a.py", "status": "modified", "patch": "@@ @@"}]
        )
    )
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/git/trees/{_HEAD_SHA}?recursive=1").mock(
        return_value=httpx.Response(
            200,
            json={
                "tree": [{"path": "a.py", "mode": "100644", "sha": "sha-a", "size": 10}],
                "truncated": True,
            },
        )
    )

    diff = await _fetch(_fetcher(test_rsa_private_key_pem))

    assert diff.files[0].mode == "100644"


@respx.mock
async def test_fetch_truncated_tree_missing_a_changed_path_is_fatal_and_logs_error(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    _mock_token_exchange()
    _mock_pr(changed_files=1)
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/files?per_page=100").mock(
        return_value=httpx.Response(
            200, json=[{"filename": "missing.py", "status": "modified", "patch": "@@ @@"}]
        )
    )
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/git/trees/{_HEAD_SHA}?recursive=1").mock(
        return_value=httpx.Response(200, json={"tree": [], "truncated": True})
    )

    with (
        caplog.at_level("ERROR", logger="adapter.github.diff"),
        pytest.raises(GitHubTreeEntryMissingError),
    ):
        await _fetch(_fetcher(test_rsa_private_key_pem))

    assert any(r.levelname == "ERROR" for r in caplog.records)


@respx.mock
async def test_fetch_non_truncated_tree_missing_a_changed_path_is_also_fatal(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    _mock_token_exchange()
    _mock_pr(changed_files=1)
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/files?per_page=100").mock(
        return_value=httpx.Response(
            200, json=[{"filename": "missing.py", "status": "modified", "patch": "@@ @@"}]
        )
    )
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/git/trees/{_HEAD_SHA}?recursive=1").mock(
        return_value=httpx.Response(200, json={"tree": [], "truncated": False})
    )

    with (
        caplog.at_level("ERROR", logger="adapter.github.diff"),
        pytest.raises(GitHubTreeEntryMissingError),
    ):
        await _fetch(_fetcher(test_rsa_private_key_pem))

    assert any(r.levelname == "ERROR" for r in caplog.records)


@respx.mock
async def test_fetch_removed_file_needs_no_tree_entry(test_rsa_private_key_pem: str) -> None:
    _mock_token_exchange()
    _mock_pr(changed_files=1)
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/files?per_page=100").mock(
        return_value=httpx.Response(200, json=[{"filename": "gone.py", "status": "removed"}])
    )
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/git/trees/{_HEAD_SHA}?recursive=1").mock(
        return_value=httpx.Response(200, json={"tree": [], "truncated": False})
    )

    diff = await _fetch(_fetcher(test_rsa_private_key_pem))

    assert diff.files[0].status == "removed"
    assert diff.files[0].blob_sha is None
    assert diff.files[0].blob_size_bytes is None


@respx.mock
async def test_fetch_unreachable_pr_call_raises_typed_error(test_rsa_private_key_pem: str) -> None:
    _mock_token_exchange()
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}").mock(
        side_effect=httpx.ConnectError("refused")
    )

    with pytest.raises(GitHubDiffFetchUnreachableError):
        await _fetch(_fetcher(test_rsa_private_key_pem, max_retry_attempts=0))


@respx.mock
async def test_fetch_goes_through_the_shared_retry_helper(test_rsa_private_key_pem: str) -> None:
    """Proves the call site actually routes through the shared retry
    helper (a transient 500 followed by success still succeeds)."""
    _mock_token_exchange()
    route = respx.get(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}")
    route.side_effect = [httpx.Response(500), httpx.Response(200, json={"changed_files": 0})]
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/pulls/{_PR_NUMBER}/files?per_page=100").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(f"{_BASE}/repos/{_OWNER_REPO}/git/trees/{_HEAD_SHA}?recursive=1").mock(
        return_value=httpx.Response(200, json={"tree": [], "truncated": False})
    )

    diff = await _fetch(_fetcher(test_rsa_private_key_pem, max_retry_attempts=3))

    assert diff.files == ()
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# fetch_content(): symlink/submodule skip, blob-size ceiling, concurrency cap
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@respx.mock
async def test_fetch_content_skips_symlink_and_submodule_entries(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    blob_route = respx.get(re.compile(rf"{re.escape(_BASE)}/repos/{_OWNER_REPO}/git/blobs/.+"))
    blob_route.mock(
        return_value=httpx.Response(200, json={"content": _b64(b"regular"), "encoding": "base64"})
    )

    diff = Diff(
        files=(
            DiffFile(
                path="link", status="added", mode="120000", blob_sha="sha-link", blob_size_bytes=5
            ),
            DiffFile(
                path="submod",
                status="added",
                mode="160000",
                blob_sha="sha-submod",
                blob_size_bytes=5,
            ),
            DiffFile(
                path="regular.py",
                status="added",
                mode="100644",
                blob_sha="sha-regular",
                blob_size_bytes=7,
            ),
        )
    )

    content = await _fetcher(test_rsa_private_key_pem).fetch_content(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        diff=diff,
        paths=["link", "submod", "regular.py"],
    )

    assert content == {"regular.py": b"regular"}
    assert blob_route.call_count == 1


@respx.mock
async def test_fetch_content_dedupes_identical_blob_sha_across_paths(
    test_rsa_private_key_pem: str,
) -> None:
    blob_route = respx.get(re.compile(rf"{re.escape(_BASE)}/repos/{_OWNER_REPO}/git/blobs/.+"))
    blob_route.mock(
        return_value=httpx.Response(200, json={"content": _b64(b"shared"), "encoding": "base64"})
    )
    _mock_token_exchange()

    diff = Diff(
        files=(
            DiffFile(
                path="a.txt", status="added", mode="100644", blob_sha="sha-dup", blob_size_bytes=6
            ),
            DiffFile(
                path="b.txt", status="added", mode="100644", blob_sha="sha-dup", blob_size_bytes=6
            ),
        )
    )

    content = await _fetcher(test_rsa_private_key_pem).fetch_content(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        diff=diff,
        paths=["a.txt", "b.txt"],
    )

    assert content == {"a.txt": b"shared", "b.txt": b"shared"}
    # One GitHub API call for the shared blob_sha, not one per path.
    assert blob_route.call_count == 1


@respx.mock
async def test_fetch_content_blob_with_no_size_is_malformed_and_never_fetched(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    blob_route = respx.get(re.compile(rf"{re.escape(_BASE)}/repos/{_OWNER_REPO}/git/blobs/.+"))
    blob_route.mock(return_value=httpx.Response(200, json={"content": _b64(b"x")}))

    diff = Diff(
        files=(
            DiffFile(
                path="mystery.bin",
                status="added",
                mode="100644",
                blob_sha="sha-mystery",
                blob_size_bytes=None,
            ),
        )
    )

    with pytest.raises(GitHubDiffFetchMalformedResponseError):
        await _fetcher(test_rsa_private_key_pem).fetch_content(
            installation_id=_INSTALLATION_ID,
            repo_full_name=_OWNER_REPO,
            diff=diff,
            paths=["mystery.bin"],
        )

    assert blob_route.call_count == 0


@respx.mock
async def test_fetch_content_blob_at_size_ceiling_is_fatal_and_never_fetched(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    _mock_token_exchange()
    blob_route = respx.get(re.compile(rf"{re.escape(_BASE)}/repos/{_OWNER_REPO}/git/blobs/.+"))
    blob_route.mock(return_value=httpx.Response(200, json={"content": _b64(b"x")}))

    diff = Diff(
        files=(
            DiffFile(
                path="huge.bin",
                status="added",
                mode="100644",
                blob_sha="sha-huge",
                blob_size_bytes=100_000_000,
            ),
        )
    )

    with (
        caplog.at_level("ERROR", logger="adapter.github.diff"),
        pytest.raises(GitHubBlobTooLargeError),
    ):
        await _fetcher(test_rsa_private_key_pem).fetch_content(
            installation_id=_INSTALLATION_ID,
            repo_full_name=_OWNER_REPO,
            diff=diff,
            paths=["huge.bin"],
        )

    assert blob_route.call_count == 0
    assert any(r.levelname == "ERROR" for r in caplog.records)


@respx.mock
async def test_fetch_content_bounded_concurrency_is_enforced(test_rsa_private_key_pem: str) -> None:
    _mock_token_exchange()
    concurrency_cap = 3
    file_count = 12
    in_flight = 0
    max_observed = 0
    lock = asyncio.Lock()

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_observed
        async with lock:
            in_flight += 1
            max_observed = max(max_observed, in_flight)
        await asyncio.sleep(0.02)
        async with lock:
            in_flight -= 1
        return httpx.Response(200, json={"content": _b64(b"x")})

    respx.get(re.compile(rf"{re.escape(_BASE)}/repos/{_OWNER_REPO}/git/blobs/.+")).mock(
        side_effect=_handler
    )

    files = tuple(
        DiffFile(
            path=f"f{i}.py", status="added", mode="100644", blob_sha=f"sha-{i}", blob_size_bytes=1
        )
        for i in range(file_count)
    )
    diff = Diff(files=files)

    await _fetcher(test_rsa_private_key_pem, blob_fetch_concurrency=concurrency_cap).fetch_content(
        installation_id=_INSTALLATION_ID,
        repo_full_name=_OWNER_REPO,
        diff=diff,
        paths=[f.path for f in files],
    )

    assert max_observed <= concurrency_cap
    assert max_observed > 1  # sanity: fetches genuinely overlapped, not accidentally serial
