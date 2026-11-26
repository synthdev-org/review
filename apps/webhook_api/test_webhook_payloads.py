"""Unit tests for the pure payload and event builders in ``webhook_routes``."""

# Also covers the bounded body reader in ``apps.webhook_api.webhook_routes``.
# There is no database and no HTTP server.

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import Request

from adapter.github.schemas import GitHubWebhookEnvelope
from apps.webhook_api import webhook_routes as routes
from tenancy.resolution import ResolutionResult, WebhookEvent

_RESULT = ResolutionResult(org_id=uuid.uuid4(), repo_id=uuid.uuid4(), is_new_delivery=True)


def _envelope(**overrides: Any) -> GitHubWebhookEnvelope:
    body: dict[str, Any] = {
        "installation": {"id": 5},
        "repository": {"full_name": "acme/w", "private": True},
    }
    body.update(overrides)
    return GitHubWebhookEnvelope.model_validate(body)


def _pr(**overrides: Any) -> dict[str, Any]:
    pr: dict[str, Any] = {
        "number": 9,
        "title": "ABC-12 fix the thing",
        "body": "closes DEF-7",
        "head": {
            "sha": "h" * 7,
            "ref": "feature/ABC-12",
            "repo": {"full_name": "acme/w", "fork": False},
        },
        "base": {"sha": "b" * 7, "repo": {"full_name": "acme/w"}},
    }
    pr.update(overrides)
    return pr


def _event(**overrides: Any) -> WebhookEvent:
    base: dict[str, Any] = {
        "delivery_id": "d-1",
        "installation_id": 5,
        "action": "opened",
        "repo_full_name": "acme/w",
        "pr_number": 9,
        "head_sha": "hhh",
        "base_sha": "bbb",
        "repo_private": True,
    }
    base.update(overrides)
    return WebhookEvent(**base)


# --- _build_webhook_event / _pr_is_fork -----------------------------------------


def test_build_webhook_event_for_a_pull_request_copies_identity_and_derives_issue_keys() -> None:
    event = routes._build_webhook_event(
        _envelope(action="opened", pull_request=_pr()), delivery_id="d-1"
    )

    assert (event.delivery_id, event.installation_id, event.action) == ("d-1", 5, "opened")
    assert (event.repo_full_name, event.repo_private) == ("acme/w", True)
    assert (event.pr_number, event.head_sha, event.base_sha) == (9, "hhhhhhh", "bbbbbbb")
    assert set(event.pr_issue_keys) == {"ABC-12", "DEF-7"}
    assert event.pr_is_fork is False


def test_build_webhook_event_for_a_push_has_no_pull_request_fields() -> None:
    event = routes._build_webhook_event(_envelope(), delivery_id="d-2")

    assert event.pr_number is None and event.head_sha is None and event.base_sha is None
    assert event.pr_title is None and event.pr_head_ref is None
    assert event.pr_issue_keys == () and event.pr_is_fork is None


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        pytest.param(
            {"sha": "h", "repo": {"full_name": "acme/w", "fork": False}}, False, id="same"
        ),
        pytest.param({"sha": "h", "repo": {"full_name": "bob/w", "fork": True}}, True, id="fork"),
        pytest.param({"sha": "h", "repo": None}, True, id="deleted-fork-fails-closed"),
        pytest.param(None, True, id="no-head-fails-closed"),
    ],
)
def test_pr_is_fork_fails_closed_on_missing_head_repo(
    head: dict[str, Any] | None, expected: bool
) -> None:
    envelope = _envelope(pull_request=_pr(head=head))

    assert routes._pr_is_fork(envelope) is expected


# --- enqueue payloads -----------------------------------------------------------


def test_review_payload_carries_identity_and_pr_context_but_never_the_body() -> None:
    event = _event(pr_title="t", pr_head_ref="r", pr_issue_keys=("ABC-1",), pr_is_fork=False)

    payload = routes._review_enqueue_payload(event, _RESULT)

    assert payload == {
        "org_id": str(_RESULT.org_id),
        "repo_id": str(_RESULT.repo_id),
        "installation_id": 5,
        "repo_full_name": "acme/w",
        "private": True,
        "pr_number": 9,
        "head_sha": "hhh",
        "base_sha": "bbb",
        "pr_title": "t",
        "pr_head_ref": "r",
        "pr_issue_keys": ["ABC-1"],
        "pr_is_fork": False,
    }


@pytest.mark.parametrize("missing", ["pr_number", "head_sha"])
def test_review_payload_is_none_without_a_required_field(missing: str) -> None:
    assert routes._review_enqueue_payload(_event(**{missing: None}), _RESULT) is None


def test_review_payload_tolerates_a_missing_base_sha() -> None:
    payload = routes._review_enqueue_payload(_event(base_sha=None), _RESULT)

    assert payload is not None and payload["base_sha"] is None


def test_memory_reaction_poll_payload_needs_only_the_pr_number() -> None:
    payload = routes._memory_reaction_poll_payload(_event(head_sha=None), _RESULT)

    assert payload is not None and payload["pr_number"] == 9 and "head_sha" not in payload
    assert routes._memory_reaction_poll_payload(_event(pr_number=None), _RESULT) is None


def test_merged_push_file_lists_dedupes_preserving_first_seen_order() -> None:
    envelope = _envelope(
        commits=[
            {"added": ["a", "b"], "removed": ["x"], "modified": ["m"]},
            {"added": ["b", "c"], "removed": [], "modified": ["m", "n"]},
        ]
    )

    assert routes._merged_push_file_lists(envelope.commits) == (
        ["a", "b", "c"],
        ["x"],
        ["m", "n"],
    )


def _index(event_type: str, envelope: GitHubWebhookEnvelope, event: WebhookEvent) -> Any:
    return routes._index_enqueue_payload(
        event_type=event_type, envelope=envelope, event=event, result=_RESULT
    )


def test_push_index_payload_merges_head_commit_and_uses_the_delta() -> None:
    envelope = _envelope(
        after="a1",
        commits=[{"added": ["a"]}],
        head_commit={"added": ["z"], "modified": ["a"]},
    )

    payload = _index("push", envelope, _event(pr_number=None, head_sha=None))

    assert payload["pr_number"] == 0 and payload["commit_sha"] == "a1"
    assert (payload["added"], payload["modified"]) == (["a", "z"], ["a"])
    assert payload["force_full_tree_fallback"] is False


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param({"forced": True}, True, id="force-push"),
        pytest.param({"commits": [{"added": ["f"]}] * 20}, True, id="truncated-commit-list"),
        pytest.param({"forced": False}, False, id="ordinary-push"),
    ],
)
def test_push_index_payload_signals_full_tree_fallback(
    overrides: dict[str, Any], expected: bool
) -> None:
    envelope = _envelope(after="a1", **overrides)

    assert _index("push", envelope, _event())["force_full_tree_fallback"] is expected


@pytest.mark.parametrize(
    "overrides", [pytest.param({"deleted": True}, id="deleted"), pytest.param({}, id="no-after")]
)
def test_push_index_payload_is_none_for_a_deletion_or_missing_after(
    overrides: dict[str, Any],
) -> None:
    envelope = _envelope(**({"after": "0" * 40} | overrides if overrides else {}))

    assert _index("push", envelope, _event()) is None


def test_pull_request_index_payload_always_forces_the_full_tree_fallback() -> None:
    payload = _index("pull_request", _envelope(), _event())

    assert payload["commit_sha"] == "hhh" and payload["pr_number"] == 9
    assert (payload["added"], payload["removed"], payload["modified"]) == ([], [], [])
    assert payload["force_full_tree_fallback"] is True
    assert _index("pull_request", _envelope(), _event(head_sha=None)) is None


# --- _reject_webhook ------------------------------------------------------------


def test_reject_webhook_logs_the_reason(caplog: pytest.LogCaptureFixture) -> None:
    routes._reject_webhook("bad_signature")

    assert [r.reason for r in caplog.records if r.message == "webhook rejected"] == [  # type: ignore[attr-defined]
        "bad_signature"
    ]


# --- _read_body_within_limit ----------------------------------------------------


def _request(chunks: list[bytes], content_length: str | None) -> Request:
    queue = [*chunks]

    async def receive() -> dict[str, Any]:
        if queue:
            return {"type": "http.request", "body": queue.pop(0), "more_body": bool(queue)}
        return {"type": "http.request", "body": b"", "more_body": False}

    headers = [(b"content-length", content_length.encode())] if content_length else []
    return Request({"type": "http", "headers": headers}, receive)


@pytest.mark.asyncio
async def test_body_reader_joins_chunks_within_the_limit() -> None:
    body = await routes._read_body_within_limit(_request([b"ab", b"cd"], "4"), max_bytes=4)

    assert body == b"abcd"


@pytest.mark.parametrize(
    ("chunks", "content_length"),
    [
        pytest.param([b"x"], "100", id="declared-too-large"),
        pytest.param([b"abc", b"de"], None, id="streamed-too-large"),
        pytest.param([b"abc", b"de"], "1", id="understated-length-header"),
    ],
)
@pytest.mark.asyncio
async def test_body_reader_rejects_oversize_bodies(
    chunks: list[bytes], content_length: str | None
) -> None:
    with pytest.raises(routes._BodyTooLargeError):
        await routes._read_body_within_limit(_request(chunks, content_length), max_bytes=4)


@pytest.mark.asyncio
async def test_body_reader_ignores_a_garbage_content_length_and_enforces_the_stream() -> None:
    body = await routes._read_body_within_limit(_request([b"ab"], "not-a-number"), max_bytes=4)

    assert body == b"ab"
    with pytest.raises(routes._BodyTooLargeError):
        await routes._read_body_within_limit(_request([b"abcde"], "not-a-number"), max_bytes=4)
