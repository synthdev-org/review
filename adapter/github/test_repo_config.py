"""Integration tests for adapter.github.repo_config against a respx
cassette (docs/plans/plan-phase-9.md Phase 2): config present, absent,
truncated tree, oversize file, fetch error -> RepoConfigFetchError (the step
degrades to no config), and only allow-listed root names are ever fetched.
"""

from __future__ import annotations

import base64
import logging
import re

import httpx
import pytest
import respx

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.repo_config import GitHubRepoConfigFetcher
from adapter.github.tree import GitHubTreeFetcher
from review.static_analysis import RepoConfigFetchError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_REPO = "acme/widgets"
_SHA = "c" * 40
_BASE = "https://api.github.com"
_TREE_URL = f"{_BASE}/repos/{_REPO}/git/trees/{_SHA}?recursive=1"
_NAMES = frozenset({"ruff.toml", ".ruff.toml"})


@pytest.fixture(autouse=True)
def _reenable_loggers() -> None:
    for name in ("adapter.github.repo_config", "adapter.github.git_data"):
        logging.getLogger(name).disabled = False


def _fetcher(pem: str) -> GitHubRepoConfigFetcher:
    provider = GitHubInstallationTokenProvider(app_id=1, private_key_pem=pem)
    return GitHubRepoConfigFetcher(GitHubTreeFetcher(token_provider=provider, max_retry_attempts=1))


def _blob(path: str, sha: str, size: int = 10, mode: str = "100644") -> dict[str, object]:
    return {"path": path, "mode": mode, "type": "blob", "sha": sha, "size": size}


def _mock_tree(entries: list[dict[str, object]], *, truncated: bool = False) -> None:
    respx.post(f"{_BASE}/app/installations/1/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "t", "expires_at": "2999-01-01T00:00:00Z"})
    )
    respx.get(_TREE_URL).mock(
        return_value=httpx.Response(200, json={"tree": entries, "truncated": truncated})
    )


async def _fetch(pem: str, max_bytes: int = 1000, max_files: int = 4) -> dict[str, bytes]:
    return await _fetcher(pem).fetch_config_files(
        installation_id=1,
        repo_full_name=_REPO,
        commit_sha=_SHA,
        names=_NAMES,
        max_file_bytes=max_bytes,
        max_files=max_files,
    )


@respx.mock
async def test_config_present_is_fetched_and_nothing_else(test_rsa_private_key_pem: str) -> None:
    _mock_tree([_blob("ruff.toml", "sha-r"), _blob("secrets.env", "sha-s"), _blob("a.py", "sha-a")])
    blobs = respx.get(re.compile(rf"{re.escape(_BASE)}/repos/{_REPO}/git/blobs/.+"))
    blobs.mock(
        return_value=httpx.Response(200, json={"content": base64.b64encode(b"x=1").decode()})
    )

    assert await _fetch(test_rsa_private_key_pem) == {"ruff.toml": b"x=1"}
    assert blobs.call_count == 1
    assert blobs.calls[0].request.url.path.endswith("/sha-r")


@respx.mock
async def test_config_absent_returns_empty_without_blob_fetch(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_tree([_blob("a.py", "sha-a")])
    blobs = respx.get(re.compile(rf"{re.escape(_BASE)}/repos/{_REPO}/git/blobs/.+"))

    assert await _fetch(test_rsa_private_key_pem) == {}
    assert blobs.call_count == 0


@respx.mock
async def test_nested_config_is_ignored_and_logged(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    _mock_tree([_blob("pkg/ruff.toml", "sha-n")])

    with caplog.at_level("INFO", logger="adapter.github.repo_config"):
        assert await _fetch(test_rsa_private_key_pem) == {}

    assert "nested analyzer config ignored" in caplog.text


@respx.mock
async def test_truncated_tree_raises_with_reason(test_rsa_private_key_pem: str) -> None:
    _mock_tree([_blob("ruff.toml", "sha-r")], truncated=True)

    with pytest.raises(RepoConfigFetchError) as err:
        await _fetch(test_rsa_private_key_pem)

    assert err.value.reason == "tree_truncated"


@respx.mock
async def test_oversize_file_is_skipped_logged_and_never_fetched(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    _mock_tree([_blob("ruff.toml", "sha-r", size=5000), _blob(".ruff.toml", "sha-d", size=10)])
    blobs = respx.get(re.compile(rf"{re.escape(_BASE)}/repos/{_REPO}/git/blobs/.+"))
    blobs.mock(return_value=httpx.Response(200, json={"content": base64.b64encode(b"ok").decode()}))

    with caplog.at_level("WARNING", logger="adapter.github.repo_config"):
        result = await _fetch(test_rsa_private_key_pem)

    assert result == {".ruff.toml": b"ok"}
    assert blobs.call_count == 1
    assert [r.__dict__.get("config_skip_reason") for r in caplog.records] == ["file_too_large"]


@respx.mock
async def test_actual_content_larger_than_reported_size_is_dropped(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_tree([_blob("ruff.toml", "sha-r", size=5)])
    respx.get(re.compile(rf"{re.escape(_BASE)}/repos/{_REPO}/git/blobs/.+")).mock(
        return_value=httpx.Response(200, json={"content": base64.b64encode(b"y" * 2000).decode()})
    )

    assert await _fetch(test_rsa_private_key_pem) == {}


@respx.mock
async def test_max_file_count_is_enforced(test_rsa_private_key_pem: str) -> None:
    _mock_tree([_blob("ruff.toml", "sha-r"), _blob(".ruff.toml", "sha-d")])
    blobs = respx.get(re.compile(rf"{re.escape(_BASE)}/repos/{_REPO}/git/blobs/.+"))
    blobs.mock(return_value=httpx.Response(200, json={"content": base64.b64encode(b"z").decode()}))

    assert len(await _fetch(test_rsa_private_key_pem, max_files=1)) == 1
    assert blobs.call_count == 1


@respx.mock
async def test_symlink_config_is_skipped(test_rsa_private_key_pem: str) -> None:
    _mock_tree([_blob("ruff.toml", "sha-l", mode="120000")])

    assert await _fetch(test_rsa_private_key_pem) == {}


@respx.mock
async def test_tree_fetch_error_raises_with_reason(test_rsa_private_key_pem: str) -> None:
    respx.post(f"{_BASE}/app/installations/1/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "t", "expires_at": "2999-01-01T00:00:00Z"})
    )
    respx.get(_TREE_URL).mock(return_value=httpx.Response(404, json={"message": "nope"}))

    with pytest.raises(RepoConfigFetchError) as err:
        await _fetch(test_rsa_private_key_pem)

    assert err.value.reason == "tree_fetch_failed"


@respx.mock
async def test_blob_fetch_error_raises_with_reason(test_rsa_private_key_pem: str) -> None:
    _mock_tree([_blob("ruff.toml", "sha-r")])
    respx.get(re.compile(rf"{re.escape(_BASE)}/repos/{_REPO}/git/blobs/.+")).mock(
        return_value=httpx.Response(404, json={"message": "nope"})
    )

    with pytest.raises(RepoConfigFetchError) as err:
        await _fetch(test_rsa_private_key_pem)

    assert err.value.reason == "content_fetch_failed"
