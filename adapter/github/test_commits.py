"""adapter.github.commits against recorded GitHub responses (respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.commits import GitHubCommitSource
from ingestion.commits import CommitsUnavailableError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_REPO = "acme/widgets"
_INSTALL = 555
_BASE = "https://api.github.com"
_COMMITS = f"{_BASE}/repos/{_REPO}/pulls/7/commits"
_COMPARE = f"{_BASE}/repos/{_REPO}/compare/aaa...ccc"


def _source(pem: str, *, retries: int = 0) -> GitHubCommitSource:
    provider = GitHubInstallationTokenProvider(app_id=1, private_key_pem=pem)
    return GitHubCommitSource(token_provider=provider, max_retry_attempts=retries)


def _token() -> None:
    respx.post(f"{_BASE}/app/installations/{_INSTALL}/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_t", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )


def _commit(sha: str, parents: list[str], msg: str) -> dict[str, object]:
    return {"sha": sha, "parents": [{"sha": p} for p in parents], "commit": {"message": msg}}


async def _list(src: GitHubCommitSource):  # type: ignore[no-untyped-def]
    return await src.list_commits(
        installation_id=_INSTALL, repo_full_name=_REPO, pr_number=7, head_sha="ccc"
    )


async def _range(src: GitHubCommitSource):  # type: ignore[no-untyped-def]
    return await src.fetch_range_diff(
        installation_id=_INSTALL, repo_full_name=_REPO, base_sha="aaa", head_sha="ccc"
    )


@respx.mock
async def test_list_commits_follows_pagination_and_keeps_title_only(
    test_rsa_private_key_pem: str,
) -> None:
    _token()
    page2 = f"{_COMMITS}?per_page=100&page=2"
    respx.get(f"{_COMMITS}?per_page=100").mock(
        return_value=httpx.Response(
            200,
            json=[_commit("c1", ["base"], "first\n\nbody")],
            headers={"Link": f'<{page2}>; rel="next"'},
        )
    )
    respx.get(page2).mock(
        return_value=httpx.Response(200, json=[_commit("c2", ["c1", "x"], "merge")])
    )

    commits = await _list(_source(test_rsa_private_key_pem))

    assert [(c.sha, c.parent_shas, c.title) for c in commits] == [
        ("c1", ("base",), "first"),
        ("c2", ("c1", "x"), "merge"),
    ]


@respx.mock
async def test_fetch_range_diff_merges_pages_and_leaves_blob_metadata_none(
    test_rsa_private_key_pem: str,
) -> None:
    _token()
    page2 = f"{_COMPARE}?per_page=100&page=2"
    respx.get(f"{_COMPARE}?per_page=100").mock(
        return_value=httpx.Response(
            200,
            json={"files": [{"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@"}]},
            headers={"Link": f'<{page2}>; rel="next"'},
        )
    )
    respx.get(page2).mock(
        return_value=httpx.Response(
            200, json={"files": [{"filename": "img.png", "status": "added"}]}
        )
    )

    diff = await _range(_source(test_rsa_private_key_pem))

    assert [(f.path, f.status, f.patch) for f in diff.files] == [
        ("a.py", "modified", "@@ -1 +1 @@"),
        ("img.png", "added", None),
    ]
    assert all(
        f.mode is None and f.blob_sha is None and f.blob_size_bytes is None for f in diff.files
    )


@respx.mock
async def test_missing_files_key_is_an_empty_diff(test_rsa_private_key_pem: str) -> None:
    _token()
    respx.get(f"{_COMPARE}?per_page=100").mock(return_value=httpx.Response(200, json={}))
    assert (await _range(_source(test_rsa_private_key_pem))).files == ()


@respx.mock
async def test_permanent_error_becomes_commits_unavailable(test_rsa_private_key_pem: str) -> None:
    _token()
    respx.get(f"{_COMMITS}?per_page=100").mock(return_value=httpx.Response(404, json={}))
    with pytest.raises(CommitsUnavailableError) as info:
        await _list(_source(test_rsa_private_key_pem))
    assert info.value.repo_full_name == _REPO


@respx.mock
async def test_exhausted_retries_become_commits_unavailable(test_rsa_private_key_pem: str) -> None:
    _token()
    route = respx.get(f"{_COMPARE}?per_page=100").mock(return_value=httpx.Response(502))
    with pytest.raises(CommitsUnavailableError):
        await _range(_source(test_rsa_private_key_pem, retries=0))
    assert route.called


@respx.mock
async def test_malformed_json_and_shapes_become_commits_unavailable(
    test_rsa_private_key_pem: str,
) -> None:
    _token()
    src = _source(test_rsa_private_key_pem)
    route = respx.get(f"{_COMMITS}?per_page=100")
    route.mock(return_value=httpx.Response(200, content=b"not json"))
    with pytest.raises(CommitsUnavailableError):
        await _list(src)
    route.mock(return_value=httpx.Response(200, json={"oops": 1}))
    with pytest.raises(CommitsUnavailableError):
        await _list(src)
    route.mock(return_value=httpx.Response(200, json=[{"sha": "x"}]))
    with pytest.raises(CommitsUnavailableError):
        await _list(src)
    respx.get(f"{_COMPARE}?per_page=100").mock(
        return_value=httpx.Response(200, json={"files": [{"status": "added"}]})
    )
    with pytest.raises(CommitsUnavailableError):
        await _range(src)


@respx.mock
async def test_token_exchange_failure_becomes_commits_unavailable(
    test_rsa_private_key_pem: str,
) -> None:
    respx.post(f"{_BASE}/app/installations/{_INSTALL}/access_tokens").mock(
        return_value=httpx.Response(500)
    )
    with pytest.raises(CommitsUnavailableError):
        await _list(_source(test_rsa_private_key_pem))


async def test_bad_repo_name_becomes_commits_unavailable(test_rsa_private_key_pem: str) -> None:
    with pytest.raises(CommitsUnavailableError):
        await _source(test_rsa_private_key_pem).list_commits(
            installation_id=_INSTALL, repo_full_name="nonsense", pr_number=1, head_sha="x"
        )
