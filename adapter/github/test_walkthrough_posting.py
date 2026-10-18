"""Tests for adapter.github.walkthrough (plan-phase-12 Phase 2, US-W4 / US-W8).

Integration tests drive ``GitHubWalkthroughPublisher`` through ``respx`` against a
small stateful fake of GitHub's issue-comment endpoints; ``is_platform_comment``
is pure and unit-tested (ADR-0005).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx

from adapter.github.auth import GitHubInstallationTokenProvider
from adapter.github.exceptions import (
    GitHubPostingMalformedRequestError,
    GitHubPostingUnreachableError,
    GitHubWalkthroughCommentLimitError,
    GitHubWalkthroughDedupeError,
)
from adapter.github.walkthrough import GitHubWalkthroughPublisher, is_platform_comment
from review.step import JobIdentity
from review.walkthrough_render import FIXED_FOOTER_LINE, MARKER

_BASE = "https://api.github.com"
_APP_ID = 77
_INSTALLATION_ID = 555
_LIST_URL = f"{_BASE}/repos/acme/widgets/issues/42/comments"
_ITEM_URL = f"{_BASE}/repos/acme/widgets/issues/comments"
_SECRET_TEXT = "TOPSECRET-BODY-TEXT"
_NO_APP: Any = object()  # sentinel: omit performed_via_github_app entirely


@pytest.fixture(autouse=True)
def _reenable_logger() -> None:
    logging.getLogger("adapter.github.walkthrough").disabled = False


def _body(text: str = "content") -> str:
    return f"{MARKER}\n\n{text}\n\n{FIXED_FOOTER_LINE}"


def _comment(cid: int, *, app: Any = None, body: str | None = None) -> dict[str, Any]:
    """A platform comment by default (``app=None`` means "our App"); pass
    ``app=_NO_APP`` to omit the field, or any other value to set it verbatim."""
    comment: dict[str, Any] = {"id": cid, "body": _body() if body is None else body}
    if app is None:
        comment["performed_via_github_app"] = {"id": _APP_ID}
    elif app is not _NO_APP:
        comment["performed_via_github_app"] = app
    return comment


def _null_app_comment(cid: int) -> dict[str, Any]:
    return {**_comment(cid), "performed_via_github_app": None}


def _identity(**overrides: Any) -> JobIdentity:
    fields: dict[str, Any] = {
        "org_id": "org",
        "repo_id": "repo",
        "installation_id": _INSTALLATION_ID,
        "repo_full_name": "acme/widgets",
        "pr_number": 42,
        "head_sha": "a" * 40,
    }
    fields.update(overrides)
    return JobIdentity(**fields)


def _publisher(pem: str, *, retries: int = 0) -> GitHubWalkthroughPublisher:
    provider = GitHubInstallationTokenProvider(app_id=1, private_key_pem=pem)
    return GitHubWalkthroughPublisher(
        token_provider=provider, app_id=_APP_ID, max_retry_attempts=retries
    )


class FakeGitHub:
    """Stateful fake of the issue-comment endpoints.

    ``list_script`` entries are consumed per GET (the last repeats): a list is
    returned verbatim, ``None`` serves the real state. ``post_script`` likewise
    with ``httpx.Response`` / ``None``. ``list_fn`` replaces GET entirely.
    ``fail`` maps a method to a status returned instead of acting.
    """

    def __init__(self, comments: list[dict[str, Any]] | None = None) -> None:
        self.comments = list(comments or [])
        self.next_id = max((c["id"] for c in self.comments), default=0) + 1
        self.fail: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []
        self.list_script: list[list[dict[str, Any]] | None] = []
        self.post_script: list[httpx.Response | None] = []
        self.list_fn: Callable[[httpx.Request], httpx.Response] | None = None
        self.list_gate: Callable[[], Awaitable[None]] | None = None
        respx.post(f"{_BASE}/app/installations/{_INSTALLATION_ID}/access_tokens").mock(
            return_value=httpx.Response(
                201, json={"token": "ghs_t", "expires_at": "2999-01-01T00:00:00Z"}
            )
        )
        respx.get(_LIST_URL).mock(side_effect=self._list)
        respx.post(_LIST_URL).mock(side_effect=self._create)
        respx.patch(url__startswith=_ITEM_URL).mock(side_effect=self._patch)
        respx.delete(url__startswith=_ITEM_URL).mock(side_effect=self._delete)

    def writes(self, method: str) -> list[str]:
        return [path for m, path in self.calls if m == method]

    @staticmethod
    def _next(script: list[Any]) -> Any:
        item: Any = script.pop(0) if len(script) > 1 else (script[0] if script else None)
        return item

    async def _list(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(("GET", request.url.path))
        scripted = self._next(self.list_script)
        snapshot = [dict(c) for c in (scripted if scripted is not None else self.comments)]
        if self.list_gate is not None:
            await self.list_gate()  # the response reflects state before the wait
        if self.list_fn is not None:
            return self.list_fn(request)
        return httpx.Response(200, json=snapshot)

    def _create(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(("POST", request.url.path))
        scripted: httpx.Response | None = self._next(self.post_script)
        if scripted is not None and scripted.status_code >= 400:
            return scripted
        if "POST" in self.fail:
            return httpx.Response(self.fail["POST"])
        cid = self.next_id
        self.next_id += 1
        self.comments.append(_comment(cid, body=json.loads(request.content)["body"]))
        return httpx.Response(201, json={"id": cid})

    def _patch(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(("PATCH", request.url.path))
        if "PATCH" in self.fail:
            return httpx.Response(self.fail["PATCH"])
        cid = int(request.url.path.rsplit("/", 1)[1])
        for c in self.comments:
            if c["id"] == cid:
                c["body"] = json.loads(request.content)["body"]
                return httpx.Response(200, json={"id": cid})
        return httpx.Response(404)

    def _delete(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(("DELETE", request.url.path))
        if "DELETE" in self.fail:
            return httpx.Response(self.fail["DELETE"])
        cid = int(request.url.path.rsplit("/", 1)[1])
        before = len(self.comments)
        self.comments = [c for c in self.comments if c["id"] != cid]
        return httpx.Response(204 if len(self.comments) < before else 404)


# --- is_platform_comment (pure; ADR-0005 unit tier) -------------------------


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        (_comment(1), True),
        (_comment(1, app=_NO_APP), False),
        (_null_app_comment(1), False),
        (_comment(1, app={"id": _APP_ID + 1}), False),
        (_comment(1, app={"id": str(_APP_ID)}), False),
        (_comment(1, app={"id": float(_APP_ID)}), False),
        (_comment(1, app={"id": True}), False),
        (_comment(1, body=f"{MARKER}\n\nno footer"), False),
        (_comment(1, body=f"intro\n{MARKER}\n{FIXED_FOOTER_LINE}"), False),
        ("not a dict", False),
        ({**_comment(1), "body": 5}, False),
    ],
)
def test_is_platform_comment_fails_closed(comment: object, expected: bool) -> None:
    assert is_platform_comment(comment, _APP_ID) is expected


def test_is_platform_comment_bool_is_not_int_one() -> None:
    assert is_platform_comment(_comment(1, app={"id": True}), 1) is False


# --- create / edit ----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_create_then_edit_in_place(test_rsa_private_key_pem: str) -> None:
    gh = FakeGitHub()
    pub = _publisher(test_rsa_private_key_pem)
    assert await pub.upsert(job_id="j", identity=_identity(), body=_body("one")) == "created"
    assert await pub.upsert(job_id="j", identity=_identity(), body=_body("two")) == "edited"
    assert len(gh.comments) == 1
    assert "two" in gh.comments[0]["body"]
    assert len(gh.writes("POST")) == 1
    assert gh.writes("DELETE") == []


def _paged(n_pages: int, *, last_has_next: bool, match_on: int | None = None) -> Any:
    """GET handler serving ``n_pages`` pages chained by ``Link: rel=next``;
    page ``match_on`` carries a platform comment (id 9)."""

    def fn(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        headers = {}
        if page < n_pages or last_has_next:
            headers["Link"] = f'<{_LIST_URL}?per_page=100&page={page + 1}>; rel="next"'
        body = [_comment(9)] if page == match_on else [{"id": 100 + page, "body": "chatter"}]
        return httpx.Response(200, json=body, headers=headers)

    return fn


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_pagination_follows_link_header_to_the_match(test_rsa_private_key_pem: str) -> None:
    gh = FakeGitHub([_comment(9)])
    gh.list_fn = _paged(3, last_has_next=False, match_on=3)
    result = await _publisher(test_rsa_private_key_pem).upsert(
        job_id="j", identity=_identity(), body=_body("new")
    )
    assert result == "edited"
    assert len(gh.writes("GET")) == 3
    assert gh.writes("PATCH") == ["/repos/acme/widgets/issues/comments/9"]


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_more_than_five_pages_without_match_raises_comment_limit(
    test_rsa_private_key_pem: str,
) -> None:
    gh = FakeGitHub()
    gh.list_fn = _paged(6, last_has_next=False)
    with pytest.raises(GitHubWalkthroughCommentLimitError):
        await _publisher(test_rsa_private_key_pem).upsert(
            job_id="j", identity=_identity(), body=_body()
        )
    assert len(gh.writes("GET")) == 5
    assert gh.writes("POST") == []
    assert gh.writes("PATCH") == []
    assert gh.writes("DELETE") == []


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_exactly_five_pages_without_match_still_creates(
    test_rsa_private_key_pem: str,
) -> None:
    gh = FakeGitHub()
    gh.list_fn = _paged(5, last_has_next=False)
    result = await _publisher(test_rsa_private_key_pem).upsert(
        job_id="j", identity=_identity(), body=_body()
    )
    assert result == "created"
    assert len(gh.writes("POST")) == 1


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_next_link_to_foreign_host_is_refused(test_rsa_private_key_pem: str) -> None:
    gh = FakeGitHub()
    evil = respx.get("https://evil.example/steal").mock(return_value=httpx.Response(200, json=[]))
    gh.list_fn = lambda _r: httpx.Response(
        200, json=[], headers={"Link": '<https://evil.example/steal>; rel="next"'}
    )
    with pytest.raises(GitHubPostingUnreachableError):
        await _publisher(test_rsa_private_key_pem).upsert(
            job_id="j", identity=_identity(), body=_body()
        )
    assert evil.call_count == 0
    assert gh.writes("POST") == []


# --- identity, fail closed ---------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "foreign",
    [
        _comment(5, app=_NO_APP),
        _null_app_comment(5),
        _comment(5, app={"id": _APP_ID + 1}),
        _comment(5, app={"id": str(_APP_ID)}),
        _comment(5, app={"id": float(_APP_ID)}),
        _comment(5, app={"id": True}),
        _comment(5, body=f"{MARKER}\n\nmissing footer"),
    ],
)
async def test_foreign_marker_comment_is_never_touched(
    test_rsa_private_key_pem: str, foreign: dict[str, Any]
) -> None:
    gh = FakeGitHub([foreign])
    result = await _publisher(test_rsa_private_key_pem).upsert(
        job_id="j", identity=_identity(), body=_body("mine")
    )
    assert result == "created"
    assert gh.writes("PATCH") == []
    assert gh.writes("DELETE") == []
    assert gh.comments[0] == foreign


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_oldest_platform_comment_wins(test_rsa_private_key_pem: str) -> None:
    gh = FakeGitHub([_comment(3), _comment(8)])
    await _publisher(test_rsa_private_key_pem).upsert(
        job_id="j", identity=_identity(), body=_body("new")
    )
    assert gh.writes("PATCH") == ["/repos/acme/widgets/issues/comments/3"]
    assert "new" in gh.comments[0]["body"]
    assert "new" not in gh.comments[1]["body"]


# --- dedupe -------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_concurrent_create_race_converges_to_one_comment(
    test_rsa_private_key_pem: str,
) -> None:
    gh = FakeGitHub()
    arrived = 0
    both = asyncio.Event()

    async def gate() -> None:
        nonlocal arrived
        if arrived < 2:  # only the two first-phase lists wait for each other
            arrived += 1
            if arrived == 2:
                both.set()
            await both.wait()

    gh.list_gate = gate
    pub = _publisher(test_rsa_private_key_pem)
    a, b = await asyncio.gather(
        pub.upsert(job_id="a", identity=_identity(), body=_body("from-a")),
        pub.upsert(job_id="b", identity=_identity(), body=_body("from-b")),
    )
    assert sorted([a, b]) == ["created", "deduped"]
    assert [c["id"] for c in gh.comments] == [1]
    # the DELETE target is the deduping caller's own POST id, nothing else
    assert gh.writes("DELETE") == ["/repos/acme/widgets/issues/comments/2"]


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_relist_never_treats_own_comment_as_older(test_rsa_private_key_pem: str) -> None:
    gh = FakeGitHub()
    await _publisher(test_rsa_private_key_pem).upsert(
        job_id="j", identity=_identity(), body=_body()
    )
    assert gh.writes("PATCH") == []
    assert gh.writes("DELETE") == []
    assert len(gh.writes("GET")) == 2  # first list + re-list


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_older_comment_not_visible_on_relist_does_nothing(
    test_rsa_private_key_pem: str,
) -> None:
    gh = FakeGitHub([_comment(1)])
    gh.list_script = [[], []]  # neither the first list nor the re-list shows comment 1
    result = await _publisher(test_rsa_private_key_pem).upsert(
        job_id="j", identity=_identity(), body=_body()
    )
    assert result == "created"
    assert gh.writes("PATCH") == []
    assert gh.writes("DELETE") == []


def _hidden_then_visible() -> list[list[dict[str, Any]] | None]:
    """First list misses the older comment (the race); the re-list shows it."""
    return [[], None]


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_dedupe_patches_older_and_deletes_own(test_rsa_private_key_pem: str) -> None:
    gh = FakeGitHub([_comment(1)])
    gh.list_script = _hidden_then_visible()
    result = await _publisher(test_rsa_private_key_pem).upsert(
        job_id="j", identity=_identity(), body=_body("fresh")
    )
    assert result == "deduped"
    assert [c["id"] for c in gh.comments] == [1]
    assert "fresh" in gh.comments[0]["body"]


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_delete_404_is_a_noop(test_rsa_private_key_pem: str) -> None:
    gh = FakeGitHub([_comment(1)])
    gh.list_script = _hidden_then_visible()
    gh.fail["DELETE"] = 404
    result = await _publisher(test_rsa_private_key_pem).upsert(
        job_id="j", identity=_identity(), body=_body()
    )
    assert result == "created"


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("failing", ["PATCH", "DELETE"])
async def test_non_404_dedupe_failure_raises_and_keeps_first_comment(
    test_rsa_private_key_pem: str, failing: str
) -> None:
    gh = FakeGitHub([_comment(1)])
    gh.list_script = _hidden_then_visible()
    gh.fail[failing] = 500
    with pytest.raises(GitHubWalkthroughDedupeError):
        await _publisher(test_rsa_private_key_pem).upsert(
            job_id="j", identity=_identity(), body=_body()
        )
    assert 2 in [c["id"] for c in gh.comments]  # the job's own first comment stays


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_relist_failure_after_create_raises_dedupe_error(
    test_rsa_private_key_pem: str,
) -> None:
    gh = FakeGitHub()
    calls = 0

    def fn(_r: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[]) if calls == 1 else httpx.Response(500)

    gh.list_fn = fn
    with pytest.raises(GitHubWalkthroughDedupeError):
        await _publisher(test_rsa_private_key_pem).upsert(
            job_id="j", identity=_identity(), body=_body()
        )
    assert len(gh.comments) == 1


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_non_json_comments_response_raises_typed_error(
    test_rsa_private_key_pem: str,
) -> None:
    gh = FakeGitHub()
    gh.list_fn = lambda _r: httpx.Response(
        200, text="<html>proxy error</html>", headers={"content-type": "text/html"}
    )
    with pytest.raises(GitHubPostingUnreachableError):
        await _publisher(test_rsa_private_key_pem).upsert(
            job_id="j", identity=_identity(), body=_body()
        )
    assert gh.comments == []


# --- safe_writes_only ----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_5xx_on_create_is_not_retried(test_rsa_private_key_pem: str) -> None:
    gh = FakeGitHub()
    gh.fail["POST"] = 502
    with pytest.raises(GitHubPostingUnreachableError):
        await _publisher(test_rsa_private_key_pem, retries=3).upsert(
            job_id="j", identity=_identity(), body=_body()
        )
    assert len(gh.writes("POST")) == 1


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_429_on_create_is_retried(test_rsa_private_key_pem: str) -> None:
    gh = FakeGitHub()
    gh.post_script = [httpx.Response(429, headers={"Retry-After": "0"}), None]
    result = await _publisher(test_rsa_private_key_pem, retries=3).upsert(
        job_id="j", identity=_identity(), body=_body()
    )
    assert result == "created"
    assert len(gh.writes("POST")) == 2
    assert len(gh.comments) == 1


# --- cancellation, validation, identity, logs, errors -----------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_cancellation_mid_flow_leaves_no_writes(test_rsa_private_key_pem: str) -> None:
    gh = FakeGitHub()

    async def slow() -> None:
        await asyncio.sleep(10)

    gh.list_gate = slow
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            _publisher(test_rsa_private_key_pem).upsert(
                job_id="j", identity=_identity(), body=_body()
            ),
            timeout=0.1,
        )
    assert gh.writes("POST") == []
    assert gh.comments == []


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_malformed_repo_full_name_is_rejected(test_rsa_private_key_pem: str) -> None:
    gh = FakeGitHub()
    with pytest.raises(GitHubPostingMalformedRequestError):
        await _publisher(test_rsa_private_key_pem).upsert(
            job_id="j", identity=_identity(repo_full_name="no-slash"), body=_body()
        )
    assert gh.calls == []


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_fork_pr_posts_on_base_repo_from_job_identity(test_rsa_private_key_pem: str) -> None:
    gh = FakeGitHub()
    await _publisher(test_rsa_private_key_pem).upsert(
        job_id="j",
        identity=_identity(pr_is_fork=True),
        body=_body("see forker/widgets#42"),
    )
    assert gh.writes("POST") == ["/repos/acme/widgets/issues/42/comments"]


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_logs_never_carry_comment_text(
    test_rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    FakeGitHub()
    caplog.set_level(logging.DEBUG)
    pub = _publisher(test_rsa_private_key_pem)
    await pub.upsert(job_id="j", identity=_identity(), body=_body(_SECRET_TEXT))
    await pub.upsert(job_id="j", identity=_identity(), body=_body(_SECRET_TEXT))
    assert caplog.records
    assert _SECRET_TEXT not in caplog.text
    assert any(getattr(r, "action", None) == "edited" for r in caplog.records)


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status", [403, 404, 500])
async def test_http_errors_map_to_typed_error_without_body(
    test_rsa_private_key_pem: str, status: int
) -> None:
    gh = FakeGitHub()
    gh.list_fn = lambda _r: httpx.Response(status, text=_SECRET_TEXT)
    with pytest.raises(GitHubPostingUnreachableError) as info:
        await _publisher(test_rsa_private_key_pem).upsert(
            job_id="j", identity=_identity(), body=_body()
        )
    assert _SECRET_TEXT not in str(info.value)


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_cassette_pins_performed_via_github_app_shape(
    test_rsa_private_key_pem: str,
) -> None:
    """GitHub's documented issue-comment shape: ``performed_via_github_app`` is
    a full App object whose ``id`` is an integer (the field the matcher keys on)."""
    gh = FakeGitHub([{"id": 1234, "body": "x"}])
    cassette = {
        "id": 1234,
        "node_id": "IC_x",
        "body": _body(),
        "user": {"login": "review[bot]", "type": "Bot"},
        "performed_via_github_app": {"id": _APP_ID, "slug": "review", "name": "review"},
    }
    gh.list_fn = lambda _r: httpx.Response(200, json=[cassette])
    result = await _publisher(test_rsa_private_key_pem).upsert(
        job_id="j", identity=_identity(), body=_body()
    )
    assert result == "edited"
    assert gh.writes("PATCH") == ["/repos/acme/widgets/issues/comments/1234"]


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "response",
    [httpx.Response(200, content=b"not json"), httpx.Response(200, json={"id": 1})],
)
async def test_a_malformed_comments_page_raises_unreachable_and_writes_nothing(
    test_rsa_private_key_pem: str, response: httpx.Response
) -> None:
    gh = FakeGitHub()
    gh.list_fn = lambda _request: response
    with pytest.raises(GitHubPostingUnreachableError):
        await _publisher(test_rsa_private_key_pem).upsert(
            job_id="j", identity=_identity(), body=_body()
        )
    assert gh.writes("POST") == []


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_an_unparseable_create_response_is_created_without_dedupe(
    test_rsa_private_key_pem: str,
) -> None:
    gh = FakeGitHub()
    respx.post(_LIST_URL).mock(return_value=httpx.Response(201, content=b"not json"))
    result = await _publisher(test_rsa_private_key_pem).upsert(
        job_id="j", identity=_identity(), body=_body()
    )
    assert result == "created"
    assert len(gh.writes("GET")) == 1  # no dedupe re-list
    assert gh.writes("DELETE") == []


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_dedupe_404_on_the_older_comment_leaves_this_jobs_comment(
    test_rsa_private_key_pem: str,
) -> None:
    gh = FakeGitHub()
    gh.next_id = 5
    # First list: nothing yet. Dedupe list: an older platform comment that has since vanished.
    gh.list_script = [[], [_comment(1)]]
    result = await _publisher(test_rsa_private_key_pem).upsert(
        job_id="j", identity=_identity(), body=_body()
    )
    assert result == "created"
    assert [c["id"] for c in gh.comments] == [5]
    assert gh.writes("DELETE") == []


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_dedupe_non_404_failure_raises_dedupe_error(test_rsa_private_key_pem: str) -> None:
    gh = FakeGitHub()
    gh.next_id = 5
    gh.list_script = [[], [_comment(1)]]
    gh.fail["PATCH"] = 403
    with pytest.raises(GitHubWalkthroughDedupeError):
        await _publisher(test_rsa_private_key_pem).upsert(
            job_id="j", identity=_identity(), body=_body()
        )
