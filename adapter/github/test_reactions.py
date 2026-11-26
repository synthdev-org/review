"""Tests for adapter.github.reactions — the concrete `ReactionFetcher`
implementation, per docs/rules/testing.md's "GitHub adapter against a
recorded cassette (respx for httpx)" convention.
"""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.reactions import GitHubReactionFetcher

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OWNER_REPO = "acme/widgets"
_INSTALLATION_ID = 555
_BASE = "https://api.github.com"


@pytest.fixture(autouse=True)
def _reenable_reactions_logger() -> None:
    """See `adapter/github/test_http.py`'s own identical fixture docstring:
    keeps every `caplog`-asserting case below deterministic regardless of
    test order."""
    logging.getLogger("adapter.github.reactions").disabled = False


def _mock_token_exchange() -> None:
    respx.post(f"{_BASE}/app/installations/{_INSTALLATION_ID}/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_test_token", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )


def _reactions_url(comment_id: int) -> str:
    return f"{_BASE}/repos/{_OWNER_REPO}/pulls/comments/{comment_id}/reactions"


def _fetcher(
    test_rsa_private_key_pem: str, *, max_retry_attempts: int = 3
) -> GitHubReactionFetcher:
    provider = GitHubInstallationTokenProvider(app_id=1, private_key_pem=test_rsa_private_key_pem)
    return GitHubReactionFetcher(token_provider=provider, max_retry_attempts=max_retry_attempts)


@respx.mock
async def test_fetch_reactions_against_a_synthetic_multi_comment_response(
    test_rsa_private_key_pem: str,
) -> None:
    """A synthetic multi-comment response — proves each comment id's own
    reactions are correctly parsed and keyed."""
    _mock_token_exchange()
    respx.get(_reactions_url(1001)).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 1, "content": "+1", "user": {"login": "alice"}},
                {"id": 2, "content": "heart", "user": {"login": "bob"}},
            ],
        )
    )
    respx.get(_reactions_url(1002)).mock(return_value=httpx.Response(200, json=[]))

    result = await _fetcher(test_rsa_private_key_pem).fetch_reactions(
        installation_id=_INSTALLATION_ID, repo_full_name=_OWNER_REPO, comment_ids=[1001, 1002]
    )

    assert set(result) == {1001, 1002}
    assert {(r.reaction_id, r.content) for r in result[1001]} == {(1, "+1"), (2, "heart")}
    assert result[1002] == ()


@respx.mock
async def test_fetch_reactions_with_no_comment_ids_makes_no_calls(
    test_rsa_private_key_pem: str,
) -> None:
    """No comment ids given -> no token exchange, no reactions call at
    all."""
    result = await _fetcher(test_rsa_private_key_pem).fetch_reactions(
        installation_id=_INSTALLATION_ID, repo_full_name=_OWNER_REPO, comment_ids=[]
    )

    assert result == {}


@respx.mock
async def test_one_comment_failing_never_aborts_the_batch(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A permanent-error response (e.g. 404, comment deleted since the last
    poll) for one comment id is logged and omitted from the returned
    mapping — the other comment's own reactions are still returned."""
    _mock_token_exchange()
    respx.get(_reactions_url(2001)).mock(return_value=httpx.Response(404))
    respx.get(_reactions_url(2002)).mock(
        return_value=httpx.Response(200, json=[{"id": 9, "content": "rocket"}])
    )

    with caplog.at_level("WARNING"):
        result = await _fetcher(test_rsa_private_key_pem, max_retry_attempts=0).fetch_reactions(
            installation_id=_INSTALLATION_ID,
            repo_full_name=_OWNER_REPO,
            comment_ids=[2001, 2002],
        )

    assert set(result) == {2002}
    assert result[2002][0].reaction_id == 9
    assert any(
        "reaction fetch failed for one comment" in record.getMessage()
        and getattr(record, "comment_id", None) == 2001
        for record in caplog.records
    )


@respx.mock
async def test_every_comment_failing_returns_an_empty_mapping(
    test_rsa_private_key_pem: str,
) -> None:
    _mock_token_exchange()
    respx.get(_reactions_url(3001)).mock(return_value=httpx.Response(404))

    result = await _fetcher(test_rsa_private_key_pem, max_retry_attempts=0).fetch_reactions(
        installation_id=_INSTALLATION_ID, repo_full_name=_OWNER_REPO, comment_ids=[3001]
    )

    assert result == {}
