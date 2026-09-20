"""Unit tests for adapter.github.triggers's declarative lookup table, per
docs/adr/0005-pure-no-io-logic-unit-tested.md — pure, no-I/O logic tested
at Unit tier despite living inside ``adapter/github``.
"""

from __future__ import annotations

import pytest

import adapter.github.triggers as triggers_module
from adapter.github.triggers import (
    is_enqueue_trigger,
    is_index_trigger,
    is_memory_capture_trigger,
    is_memory_reaction_poll_trigger,
)

_TRIGGERING_PAIRS = [
    ("pull_request", "opened"),
    ("pull_request", "synchronize"),
    ("pull_request", "reopened"),
]

_NON_TRIGGERING_PAIRS = [
    ("pull_request", "closed"),
    ("pull_request", "labeled"),
    ("pull_request", "edited"),
    ("push", "opened"),
    ("issues", "opened"),
    ("check_run", "completed"),
]


@pytest.mark.parametrize(("event_type", "action"), _TRIGGERING_PAIRS)
def test_mapped_pairs_trigger_enqueue(event_type: str, action: str) -> None:
    assert is_enqueue_trigger(event_type, action) is True


@pytest.mark.parametrize(("event_type", "action"), _NON_TRIGGERING_PAIRS)
def test_unmapped_pairs_do_not_trigger_enqueue(event_type: str, action: str) -> None:
    assert is_enqueue_trigger(event_type, action) is False


def test_none_action_never_triggers_enqueue() -> None:
    """A real `push` payload has no top-level `action` key at all — proves
    the `None` case is handled explicitly, not just accidentally falling
    through to "no match" via a failed tuple membership check."""
    assert is_enqueue_trigger("push", None) is False
    assert is_enqueue_trigger("pull_request", None) is False


def test_a_hypothetical_new_entry_can_be_added_without_touching_the_lookup_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves a later trigger (e.g. a comment-triggered re-review) is a new
    set entry, with zero change to `is_enqueue_trigger` itself."""
    extended = triggers_module._ENQUEUE_REVIEW_TRIGGERS | {("pull_request", "edited")}
    monkeypatch.setattr(triggers_module, "_ENQUEUE_REVIEW_TRIGGERS", extended)

    assert is_enqueue_trigger("pull_request", "edited") is True


# --- is_index_trigger (indexing-delivery plan's Phase 1) ---


def test_push_to_default_branch_triggers_index() -> None:
    assert is_index_trigger("push", None, "refs/heads/main", "main") is True


def test_push_to_non_default_branch_does_not_trigger_index() -> None:
    assert is_index_trigger("push", None, "refs/heads/feature-x", "main") is False


def test_push_missing_ref_does_not_trigger_index() -> None:
    """A malformed/adversarial `push` delivery missing `ref` resolves to
    "no match" rather than raising or guessing."""
    assert is_index_trigger("push", None, None, "main") is False


def test_push_missing_default_branch_does_not_trigger_index() -> None:
    """`repository.default_branch` is optional per
    `adapter/github/schemas.py` — a delivery missing it can never match,
    never crash."""
    assert is_index_trigger("push", None, "refs/heads/main", None) is False


@pytest.mark.parametrize("action", ["opened", "synchronize"])
def test_pull_request_opened_or_synchronize_triggers_index(action: str) -> None:
    assert is_index_trigger("pull_request", action, None, None) is True


@pytest.mark.parametrize("action", ["reopened", "closed", "labeled", "edited"])
def test_other_pull_request_actions_do_not_trigger_index(action: str) -> None:
    """`reopened` triggers a `REVIEW` (per `_ENQUEUE_REVIEW_TRIGGERS`) but
    never an `INDEX` — reopening a PR with no new commits has nothing new
    to index, proving the two trigger tables are independent, not aliases
    of each other."""
    assert is_index_trigger("pull_request", action, None, None) is False


def test_pull_request_none_action_does_not_trigger_index() -> None:
    assert is_index_trigger("pull_request", None, None, None) is False


@pytest.mark.parametrize("event_type", ["ping", "installation", "issues", "check_run"])
def test_other_event_types_never_trigger_index(event_type: str) -> None:
    assert is_index_trigger(event_type, "opened", "refs/heads/main", "main") is False


def test_a_hypothetical_new_pull_request_index_action_can_be_added_without_touching_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extended = triggers_module._ENQUEUE_INDEX_PULL_REQUEST_TRIGGERS | {("pull_request", "reopened")}
    monkeypatch.setattr(triggers_module, "_ENQUEUE_INDEX_PULL_REQUEST_TRIGGERS", extended)

    assert is_index_trigger("pull_request", "reopened", None, None) is True


# --- is_memory_capture_trigger (plan-deliver-memory.md's Phase 3) ---

_MEMORY_CAPTURE_TRIGGERING_PAIRS = [
    ("pull_request_review_comment", "created"),
    ("pull_request_review_thread", "resolved"),
    ("pull_request_review_thread", "unresolved"),
]

_MEMORY_CAPTURE_NON_TRIGGERING_PAIRS = [
    ("pull_request_review_comment", "edited"),
    ("pull_request_review_comment", "deleted"),
    ("pull_request_review_thread", "created"),
    # Assumption 3: `issue_comment` never correlates to a specific posted
    # finding comment, so it's never a capture trigger even for `created`.
    ("issue_comment", "created"),
    ("pull_request", "closed"),
    ("push", "opened"),
]


@pytest.mark.parametrize(("event_type", "action"), _MEMORY_CAPTURE_TRIGGERING_PAIRS)
def test_mapped_pairs_trigger_memory_capture(event_type: str, action: str) -> None:
    assert is_memory_capture_trigger(event_type, action) is True


@pytest.mark.parametrize(("event_type", "action"), _MEMORY_CAPTURE_NON_TRIGGERING_PAIRS)
def test_unmapped_pairs_do_not_trigger_memory_capture(event_type: str, action: str) -> None:
    assert is_memory_capture_trigger(event_type, action) is False


def test_none_action_never_triggers_memory_capture() -> None:
    assert is_memory_capture_trigger("pull_request_review_comment", None) is False
    assert is_memory_capture_trigger("pull_request_review_thread", None) is False


def test_a_hypothetical_new_memory_capture_trigger_can_be_added_without_touching_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extended = triggers_module._MEMORY_CAPTURE_TRIGGERS | {("issue_comment", "created")}
    monkeypatch.setattr(triggers_module, "_MEMORY_CAPTURE_TRIGGERS", extended)

    assert is_memory_capture_trigger("issue_comment", "created") is True


# --- is_memory_reaction_poll_trigger (plan-deliver-memory.md's Phase 4) ---

_MEMORY_REACTION_POLL_TRIGGERING_PAIRS = [
    ("pull_request", "opened"),
    ("pull_request", "synchronize"),
    ("pull_request", "reopened"),
    ("pull_request", "closed"),
]

_MEMORY_REACTION_POLL_NON_TRIGGERING_PAIRS = [
    ("pull_request", "labeled"),
    ("pull_request", "edited"),
    ("push", "opened"),
    ("issue_comment", "created"),
    ("pull_request_review_comment", "created"),
]


@pytest.mark.parametrize(("event_type", "action"), _MEMORY_REACTION_POLL_TRIGGERING_PAIRS)
def test_mapped_pairs_trigger_memory_reaction_poll(event_type: str, action: str) -> None:
    assert is_memory_reaction_poll_trigger(event_type, action) is True


@pytest.mark.parametrize(("event_type", "action"), _MEMORY_REACTION_POLL_NON_TRIGGERING_PAIRS)
def test_unmapped_pairs_do_not_trigger_memory_reaction_poll(event_type: str, action: str) -> None:
    assert is_memory_reaction_poll_trigger(event_type, action) is False


def test_closed_never_triggers_review_or_index() -> None:
    """`closed` is unique to the reaction-poll trigger set — it must never
    also match `is_enqueue_trigger`/`is_index_trigger`, or a PR-closed event
    would start enqueuing REVIEW/INDEX jobs for a PR nothing is reviewing
    anymore."""
    assert is_enqueue_trigger("pull_request", "closed") is False
    assert is_index_trigger("pull_request", "closed", None, None) is False
    assert is_memory_reaction_poll_trigger("pull_request", "closed") is True


def test_none_action_never_triggers_memory_reaction_poll() -> None:
    assert is_memory_reaction_poll_trigger("pull_request", None) is False


def test_a_hypothetical_new_memory_reaction_poll_trigger_can_be_added_without_touching_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extended = triggers_module._MEMORY_REACTION_POLL_TRIGGERS | {("pull_request", "edited")}
    monkeypatch.setattr(triggers_module, "_MEMORY_REACTION_POLL_TRIGGERS", extended)

    assert is_memory_reaction_poll_trigger("pull_request", "edited") is True
