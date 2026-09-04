"""adapter.github.triggers — declarative mapping of GitHub webhook
``(event_type, action)`` pairs to "this should enqueue a review", per the
plan's Phase 1 section.

Deliberately a plain ``frozenset[tuple[str, str]]`` membership check, not a
``dict`` keyed to a richer decision type — every populated entry today
resolves to the identical outcome (enqueue a review), so a dict buys
nothing over a set; see the plan's own "simplified from an earlier draft's
``dict[..., TriggerDecision]`` mapping" note. If a future trigger needs a
genuinely different outcome than "enqueue a review" (not just a different
event/action pair), that's the point at which a richer value type earns its
keep — not before.

Lives in ``adapter/github`` (not ``tenancy``) deliberately:
``pull_request``/``synchronize``/``reopened`` are GitHub's own event/action
vocabulary, not a git-host-neutral concept — a future GitLab adapter
defines its own trigger vocabulary in its own ``adapter/gitlab/triggers.py``
with zero changes needed here or in the domain, per architecture.md's "new
adapter = zero domain changes" Decoupling rule.

Per ``docs/adr/0005-pure-no-io-logic-unit-tested.md``: this table (and the
lookup function below) is pure, no-I/O logic, tested at Unit tier
(``adapter/github/test_triggers.py``) despite living inside ``adapter``,
which testing.md otherwise assigns wholesale to the Integration tier.
"""

from __future__ import annotations

_ENQUEUE_REVIEW_TRIGGERS: frozenset[tuple[str, str]] = frozenset(
    {
        ("pull_request", "opened"),
        ("pull_request", "synchronize"),
        ("pull_request", "reopened"),
    }
)


def is_enqueue_trigger(event_type: str, action: str | None) -> bool:
    """Whether this ``(event_type, action)`` pair should enqueue a review.

    ``action`` is ``None`` for event types that carry no top-level
    ``action`` key at all (e.g. a real ``push`` payload) — always "no
    match" here, matching ``GitHubWebhookEnvelope.action``'s own
    optionality (``adapter/github/schemas.py``). Every ``(event_type,
    action)`` combination absent from the set resolves to "accept, don't
    enqueue" by default — the same default this function applies to a
    ``None`` action.
    """
    if action is None:
        return False
    return (event_type, action) in _ENQUEUE_REVIEW_TRIGGERS
