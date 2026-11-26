"""Unit tests: `apps.webhook_api.webhook_routes._handle_memory_capture`'s
per-candidate isolation logic in the `pull_request_review_thread` branch —
a `Fake` at the `memory.store.MemorySignalRecorder` Protocol boundary, no
real I/O, per `docs/rules/testing.md`'s "Unit... Fakes at consumer-defined
`typing.Protocol` boundaries" convention.

`apps/webhook_api/test_webhook_routes.py` already covers this route's
user-story-level behavior end-to-end against a real Postgres (redelivery
idempotency, thread-toggle, ceiling-exceeded, ...); this file covers the
one branch that's impractical to trigger deterministically through that
same real-Postgres stack — a genuine mid-batch `MemoryStoreError` on one
specific thread candidate but not another — without depending on
`record_signal`'s own concrete Postgres wiring at all, per this route's
own `MemorySignalRecorder` Protocol boundary.
"""

from __future__ import annotations

import uuid

import pytest

from apps.webhook_api.rate_limiter import InMemoryRateLimiter
from apps.webhook_api.webhook_routes import _handle_memory_capture
from findings.redact import NoOpTextRedactor
from memory.store import MemoryStoreError

pytestmark = pytest.mark.asyncio

_REPO_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


class _FlakySignalRecorder:
    """A `memory.store.MemorySignalRecorder` fake whose `record_signal`
    raises `MemoryStoreError` for a caller-chosen set of
    `posted_finding_comment_id`s and returns `True` (found-and-recorded)
    for every other id — lets a test force a failure on one specific
    thread candidate without the others being affected.
    """

    def __init__(self, *, fails_for: set[int]) -> None:
        self._fails_for = fails_for
        self.recorded_ids: list[int] = []

    async def record_comment_provenance(self, **_: object) -> None:
        raise AssertionError("not exercised by _handle_memory_capture")

    async def record_signal(
        self,
        *,
        posted_finding_comment_id: int,
        raw_signal_type: str,
        raw_signal_detail: dict[str, object],
        github_comment_id: int | None,
    ) -> bool:
        if posted_finding_comment_id in self._fails_for:
            raise MemoryStoreError(operation="record_signal", detail="simulated Postgres failure")
        self.recorded_ids.append(posted_finding_comment_id)
        return True


def _thread_payload(*, action: str = "resolved", comment_ids: list[int]) -> dict[str, object]:
    return {
        "action": action,
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 7},
        "thread": {"comments": [{"id": comment_id} for comment_id in comment_ids]},
    }


def _ceiling(*, max_requests: int = 500) -> InMemoryRateLimiter:
    return InMemoryRateLimiter(max_requests=max_requests, window_seconds=86_400.0)


async def test_one_candidates_memory_store_error_does_not_discard_an_earlier_success() -> None:
    """Regression test for the bug a review pass found: a `MemoryStoreError`
    on a later thread candidate used to be caught by the function-level
    `except MemoryStoreError`, reporting `"capture_failed"` for the whole
    event even though an earlier candidate in the same thread had already
    durably committed its own `memory_entry` row in a separate transaction.
    """
    recorder = _FlakySignalRecorder(fails_for={4002})

    outcome = await _handle_memory_capture(
        raw_payload=_thread_payload(comment_ids=[4001, 4002]),
        event_type="pull_request_review_thread",
        action="resolved",
        repo_id=_REPO_ID,
        provenance_recorder=recorder,
        text_redactor=NoOpTextRedactor(),
        capture_ceiling_limiter=_ceiling(),
    )

    assert outcome == "captured"
    assert recorder.recorded_ids == [4001]


async def test_every_candidate_failing_reports_capture_failed() -> None:
    recorder = _FlakySignalRecorder(fails_for={4001, 4002})

    outcome = await _handle_memory_capture(
        raw_payload=_thread_payload(comment_ids=[4001, 4002]),
        event_type="pull_request_review_thread",
        action="resolved",
        repo_id=_REPO_ID,
        provenance_recorder=recorder,
        text_redactor=NoOpTextRedactor(),
        capture_ceiling_limiter=_ceiling(),
    )

    assert outcome == "capture_failed"
    assert recorder.recorded_ids == []


async def test_ceiling_bounds_each_thread_candidate_not_just_the_whole_event() -> None:
    """Regression test for the bug a review pass found: the per-repo daily
    ceiling used to be checked once per webhook event, but the thread
    branch performs one `record_signal` write per correlating candidate —
    so a thread with several candidates could consume 1 unit of the ceiling
    while performing N writes. The ceiling must be checked (and able to
    reject) once per candidate.
    """
    recorder = _FlakySignalRecorder(fails_for=set())
    # Exactly 1 capture attempt allowed — the first candidate consumes it,
    # the second must be rejected by the ceiling rather than written.
    ceiling = _ceiling(max_requests=1)

    outcome = await _handle_memory_capture(
        raw_payload=_thread_payload(comment_ids=[4001, 4002]),
        event_type="pull_request_review_thread",
        action="resolved",
        repo_id=_REPO_ID,
        provenance_recorder=recorder,
        text_redactor=NoOpTextRedactor(),
        capture_ceiling_limiter=ceiling,
    )

    # An earlier candidate already succeeded, so the overall outcome is
    # still "captured" (a partial per-thread outcome is never reported as a
    # total rejection) — but only the first candidate was ever written.
    assert outcome == "captured"
    assert recorder.recorded_ids == [4001]


async def test_ceiling_exceeded_before_any_candidate_reports_capture_ceiling_exceeded() -> None:
    recorder = _FlakySignalRecorder(fails_for=set())
    ceiling = _ceiling(max_requests=1)
    # Exhaust the single allowed slot before calling _handle_memory_capture
    # at all, so even the first candidate is rejected by the ceiling.
    ceiling.allow(str(_REPO_ID))

    outcome = await _handle_memory_capture(
        raw_payload=_thread_payload(comment_ids=[4001, 4002]),
        event_type="pull_request_review_thread",
        action="resolved",
        repo_id=_REPO_ID,
        provenance_recorder=recorder,
        text_redactor=NoOpTextRedactor(),
        capture_ceiling_limiter=ceiling,
    )

    assert outcome == "capture_ceiling_exceeded"
    assert recorder.recorded_ids == []
