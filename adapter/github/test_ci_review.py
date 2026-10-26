"""Unit tests for ``adapter.github.ci_review``'s pure logic and the static token provider; the
stories themselves are integration-tested in ``apps/local_cli/test_ci.py``."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from adapter.github.ci_review import (
    CI_COMMENT_MARKER,
    _comment_key,
    _finding_key,
    _sanitize,
    is_ci_comment,
)
from adapter.github.diff import GitHubDiffFetcher
from adapter.github.reactions import GitHubReactionFetcher
from adapter.github.token_provider import InstallationTokenProvider, StaticTokenProvider
from adapter.github.tree import GitHubTreeFetcher
from adapter.github.walkthrough import GitHubWalkthroughPublisher
from findings.markdown import MESSAGE_CAP
from findings.models import Finding, Suggestion
from review.step import JobIdentity

BOT = "github-actions[bot]"


def _comment(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 1,
        "user": {"login": BOT},
        "body": f"text\n\n{CI_COMMENT_MARKER}",
        "path": "a.py",
        "line": 3,
    }
    return {**base, **overrides}


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        (_comment(), True),
        (_comment(user={"login": "mallory"}), False),  # copied marker, wrong author
        (_comment(user={"login": "github-actions[bot]x"}), False),
        (_comment(user=None), False),
        (_comment(body="text"), False),  # right author, no marker
        (_comment(body=f"quoted: {CI_COMMENT_MARKER} inline"), False),  # marker not on its own line
        (_comment(body=None), False),
        (_comment(id=True), False),
        (_comment(id="1"), False),
        (_comment(in_reply_to_id=5), False),
        ("not a dict", False),
    ],
)
def test_is_ci_comment_needs_the_bot_login_and_the_marker(comment: object, expected: bool) -> None:
    assert is_ci_comment(comment, BOT) is expected


def test_comment_key_uses_the_range_and_is_none_once_outdated() -> None:
    assert _comment_key(_comment()) == ("a.py", 3, 3)
    assert _comment_key(_comment(start_line=1, line=3)) == ("a.py", 1, 3)
    # GitHub nulls `line` once a push moves the code: it no longer covers any finding.
    assert _comment_key(_comment(line=None, original_line=9)) is None
    assert _comment_key(_comment(line=None)) is None
    assert _comment_key(_comment(line=True)) is None


def test_finding_key_is_the_suggestion_range_else_the_line_else_none() -> None:
    plain = Finding(file="a.py", line=3, severity="low", message="m", source="llm")
    ranged = plain.model_copy(
        update={"suggestion": Suggestion(start_line=2, end_line=4, replacement="x")}
    )
    file_level = plain.model_copy(update={"line": None})
    assert _finding_key(plain) == ("a.py", 3, 3)
    assert _finding_key(ranged) == ("a.py", 2, 4)
    assert _finding_key(file_level) is None


def test_sanitize_collapses_whitespace_breaks_links_and_caps_the_length() -> None:
    finding = Finding(
        file="a.py",
        line=1,
        severity="low",
        source="llm",
        message="see https://x.example\nand www.y.example\x1b[31m " + "z" * (MESSAGE_CAP * 2),
        rule_id="r\n1",
    )

    clean = _sanitize(finding)

    assert "\n" not in clean.message and "\x1b" not in clean.message
    assert "https://" not in clean.message and "www.y" not in clean.message
    assert len(clean.message) == MESSAGE_CAP
    assert clean.rule_id == "r 1"
    assert finding.message.startswith("see https://")  # the original is untouched


@pytest.mark.asyncio
async def test_static_token_provider_returns_its_token_for_any_installation_or_repo() -> None:
    provider = StaticTokenProvider(SecretStr("s3cret-value"))

    first = await provider.get_token(installation_id=0, repo_full_name="a/b")
    second = await provider.get_token(installation_id=99, repo_full_name="c/d")

    assert first.get_secret_value() == second.get_secret_value() == "s3cret-value"
    assert "s3cret-value" not in repr(provider)


# Every client whose constructor was widened accepts the static provider and sends its token.


def test_every_widened_constructor_accepts_a_static_token_provider() -> None:
    provider: InstallationTokenProvider = StaticTokenProvider(SecretStr("s3cret-value"))
    for build in (
        GitHubDiffFetcher,
        GitHubTreeFetcher,
        GitHubReactionFetcher,
    ):
        assert build(token_provider=provider) is not None
    assert GitHubWalkthroughPublisher(token_provider=provider, app_id=1) is not None


@pytest.mark.asyncio
async def test_walkthrough_publisher_sends_the_static_token() -> None:
    identity = JobIdentity(
        org_id="o",
        repo_id="r",
        installation_id=0,
        repo_full_name="acme/widgets",
        pr_number=7,
        head_sha="a" * 40,
    )
    publisher = GitHubWalkthroughPublisher(
        token_provider=StaticTokenProvider(SecretStr("static-tok")), app_id=1, max_retry_attempts=0
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(path="/repos/acme/widgets/issues/7/comments").respond(200, json=[])
        create = router.post(path="/repos/acme/widgets/issues/7/comments").respond(
            200, json={"id": 5}
        )

        action = await publisher.upsert(job_id="j", identity=identity, body="hello")

    assert action == "created"
    assert create.calls.last.request.headers["authorization"] == "Bearer static-tok"
    assert json.loads(create.calls.last.request.content) == {"body": "hello"}
    assert isinstance(create.calls.last.response, httpx.Response)


def test_the_existing_installation_provider_still_satisfies_the_widened_protocol() -> None:
    from adapter.github.auth import GitHubInstallationTokenProvider

    provider: InstallationTokenProvider = GitHubInstallationTokenProvider(
        app_id=1, private_key_pem="unused"
    )
    assert provider is not None


def test_the_ci_comment_counts_and_the_run_summary_carry_the_same_fields() -> None:
    import dataclasses

    from adapter.github.ci_review import CiCommentCounts
    from adapter.local.summary import CiCommentsSummary

    assert {f.name for f in dataclasses.fields(CiCommentCounts)} == set(
        CiCommentsSummary.model_fields
    )


# --- the publisher's GitHub failure handling, driven directly against a respx cassette ---------

from adapter.github.ci_review import GitHubCiReviewPublisher  # noqa: E402

API = "https://api.github.com"
REPO = "/repos/acme/widgets"


def _publisher(repo: str = "acme/widgets", **overrides: object) -> GitHubCiReviewPublisher:
    return GitHubCiReviewPublisher(
        token_provider=StaticTokenProvider(SecretStr("tok")),
        repo_full_name=repo,
        pr_number=7,
        bot_login=BOT,
        max_retry_attempts=1,
        **overrides,  # type: ignore[arg-type]
    )


async def _publish(publisher: GitHubCiReviewPublisher, findings: list[Finding]) -> None:
    await publisher.post_review(
        installation_id=0,
        repo_full_name="acme/widgets",
        repo_id="local",
        pr_number=7,
        head_sha="a" * 40,
        findings=findings,
    )


def _own(**overrides: object) -> dict[str, object]:
    return _comment(node_id="N1", **overrides)


_FINDING = Finding(file="a.py", line=3, severity="low", message="m", source="llm")
_FILE_LEVEL = Finding(file="a.py", line=None, severity="low", message="m", source="llm")


@pytest.mark.asyncio
async def test_a_repository_name_that_is_not_owner_slash_repo_is_recorded_not_raised() -> None:
    publisher = _publisher(repo="nonsense")

    await _publish(publisher, [_FINDING])

    assert publisher.failure == ("unavailable", "repository name is not owner/repo")


@pytest.mark.asyncio
async def test_an_unexpected_error_while_reconciling_is_recorded_not_raised() -> None:
    class Boom:
        async def get_token(self, *, installation_id: int, repo_full_name: str) -> SecretStr:
            raise RuntimeError("no token")

    publisher = _publisher()
    publisher._token_provider = Boom()  # type: ignore[assignment]

    await _publish(publisher, [_FINDING])

    assert publisher.failure == ("unavailable", "reconciling comments: RuntimeError")


@pytest.mark.asyncio
async def test_the_check_run_reconciles_when_no_review_was_posted() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(path=f"{REPO}/pulls/7/comments").respond(200, json=[_own()])
        graphql = router.post(path="/graphql").respond(200, json={"data": {}})
        router.patch(path__regex=rf"{REPO}/pulls/comments/\d+").respond(200, json={})
        publisher = _publisher()

        await publisher.post_check_run(
            installation_id=0, repo_full_name="acme/widgets", head_sha="a" * 40, finding_count=0
        )
        await publisher.post_check_run(
            installation_id=0, repo_full_name="acme/widgets", head_sha="a" * 40, finding_count=0
        )

    assert publisher.counts.outdated == 1
    assert graphql.call_count == 1  # the second check run does not reconcile again


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listing", "detail"),
    [
        (httpx.Response(200, json={"not": "a list"}), "listing review comments: not a list"),
        (httpx.Response(200, content=b"<html>"), "listing review comments: not a list"),
        (
            httpx.Response(200, json=[], headers={"Link": '<https://evil.example/x>; rel="next"'}),
            "listing review comments: foreign page link",
        ),
    ],
)
async def test_a_malformed_or_foreign_listing_posts_nothing_and_is_recorded(
    listing: httpx.Response, detail: str
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(path=f"{REPO}/pulls/7/comments").mock(return_value=listing)
        review = router.post(path=f"{REPO}/pulls/7/reviews").respond(200, json={"id": 1})
        publisher = _publisher()

        await _publish(publisher, [_FINDING])

    assert publisher.failure == ("unavailable", detail)
    assert publisher.counts.dedupe_failure == detail
    assert not review.called


@pytest.mark.asyncio
async def test_a_page_limit_breach_posts_nothing_and_is_recorded() -> None:
    own_next = {"Link": f'<{API}{REPO}/pulls/7/comments?page=2>; rel="next"'}
    with respx.mock(assert_all_called=False) as router:
        router.get(path=f"{REPO}/pulls/7/comments").mock(
            return_value=httpx.Response(200, json=[], headers=own_next)
        )
        publisher = _publisher()

        await _publish(publisher, [_FINDING])

    assert publisher.failure is not None
    assert "more than 50 pages" in publisher.failure[1]


@pytest.mark.asyncio
async def test_a_failed_review_listing_for_a_file_level_finding_posts_nothing() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(path=f"{REPO}/pulls/7/comments").respond(200, json=[])
        router.get(path=f"{REPO}/pulls/7/reviews").respond(403)
        review = router.post(path=f"{REPO}/pulls/7/reviews").respond(200, json={"id": 1})
        publisher = _publisher()

        await _publish(publisher, [_FILE_LEVEL])

    assert publisher.failure == ("permission", "listing review comments: HTTP 403")
    assert not review.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("graphql", "comment", "expected"),
    [
        (
            httpx.Response(200, json={"errors": [{"type": "FORBIDDEN"}]}),
            _own(),
            ("permission", "marking a review comment outdated: GraphQL error"),
        ),
        (
            httpx.Response(200, json={"errors": [{"type": "RATE_LIMITED"}]}),
            _own(),
            ("unavailable", "marking a review comment outdated: GraphQL error"),
        ),
        (
            httpx.Response(200, content=b"<html>"),
            _own(),
            ("unavailable", "marking a review comment outdated: GraphQL error"),
        ),
        (
            httpx.Response(200, json={"errors": "denied"}),
            _own(),
            ("unavailable", "marking a review comment outdated: GraphQL error"),
        ),
        (
            httpx.Response(200, json={}),
            _comment(),
            ("unavailable", "an own comment has no node id"),
        ),
    ],
)
async def test_an_outdate_the_server_refuses_is_recorded_with_the_right_kind(
    graphql: httpx.Response, comment: dict[str, object], expected: tuple[str, str]
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(path=f"{REPO}/pulls/7/comments").respond(200, json=[comment])
        router.post(path="/graphql").mock(return_value=graphql)
        patch = router.patch(path__regex=rf"{REPO}/pulls/comments/\d+").respond(200, json={})
        publisher = _publisher()

        await _publish(publisher, [])

    assert publisher.failure == expected
    assert publisher.counts.outdated == 0
    assert not patch.called  # an unminimised comment is never tagged outdated


@pytest.mark.asyncio
async def test_a_failed_outdated_tag_does_not_fail_the_run() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(path=f"{REPO}/pulls/7/comments").respond(200, json=[_own()])
        router.post(path="/graphql").respond(200, json={"data": {}})
        router.patch(path__regex=rf"{REPO}/pulls/comments/\d+").respond(500)
        publisher = _publisher()

        await _publish(publisher, [])

    assert publisher.failure is None
    assert publisher.counts.outdated == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, ("permission", "posting the review: HTTP 403")),
        (404, ("permission", "posting the review: HTTP 404")),
        (500, ("unavailable", "posting the review: HTTP 500")),
    ],
)
async def test_a_refused_review_post_is_recorded_with_the_right_kind(
    status: int, expected: tuple[str, str]
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(path=f"{REPO}/pulls/7/comments").respond(200, json=[])
        router.post(path=f"{REPO}/pulls/7/reviews").respond(status)
        publisher = _publisher()

        await _publish(publisher, [_FINDING])

    assert publisher.failure == expected
    assert publisher.counts.posted == 0


@pytest.mark.asyncio
async def test_an_unexpected_error_while_posting_is_recorded_not_raised() -> None:
    publisher = _publisher()

    async def boom(**_: object) -> None:
        raise RuntimeError("boom")

    publisher._inner.post_review = boom  # type: ignore[method-assign,assignment]
    with respx.mock(assert_all_called=False) as router:
        router.get(path=f"{REPO}/pulls/7/comments").respond(200, json=[])

        await _publish(publisher, [_FINDING])

    assert publisher.failure == ("unavailable", "posting the review: RuntimeError")


@pytest.mark.asyncio
async def test_a_failed_head_lookup_counts_as_a_moved_head() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(path=f"{REPO}/pulls/7/comments").respond(200, json=[])
        router.post(path=f"{REPO}/pulls/7/reviews").respond(422)
        router.get(path=f"{REPO}/pulls/7").respond(200, json={"unexpected": "shape"})
        publisher = _publisher()

        await _publish(publisher, [_FINDING, _FINDING.model_copy(update={"line": 5})])

    assert publisher.failure is not None
    assert "head is no longer the commit under review" in publisher.failure[1]
