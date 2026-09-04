"""Unit tests for adapter.github.triggers's declarative lookup table, per
docs/adr/0005-pure-no-io-logic-unit-tested.md — pure, no-I/O logic tested
at Unit tier despite living inside ``adapter/github``.
"""

from __future__ import annotations

import pytest

import adapter.github.triggers as triggers_module
from adapter.github.triggers import is_enqueue_trigger

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
