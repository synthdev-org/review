"""adapter.github.triggers — declarative mapping of GitHub webhook
``(event_type, action)`` pairs to "this should enqueue a review", per the
plan's Phase 1 section. Also home to ``is_index_trigger`` (this plan's
indexing-delivery Phase 1), the equivalent decision for "this should
enqueue an ``INDEX`` job"; ``is_memory_capture_trigger``
(``docs/plans/plan-deliver-memory.md``'s Phase 3), the equivalent decision
for "this should attempt to capture a reply/thread-resolution raw signal";
and ``is_memory_reaction_poll_trigger`` (that same plan's Phase 4, Unit D,
Assumption 4), the equivalent decision for "this should enqueue a
``MEMORY_REACTION_POLL`` job" — four independent pure functions, not one
folded into another, since each answers a genuinely different question
with its own trigger table (see each function's own docstring).

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

# Per the plan's Phase 1 section: `pull_request` `INDEX`-triggering actions
# are the same two `opened`/`synchronize` actions `_ENQUEUE_REVIEW_TRIGGERS`
# already matches — "a different job type, same trigger moment" — but
# deliberately its own set rather than reusing `_ENQUEUE_REVIEW_TRIGGERS`
# directly: the two trigger tables answer genuinely different questions
# (`reopened` enqueues a review but never an index reindex, since reopening
# a PR with no new commits has nothing new to index) and are free to diverge
# further without either becoming a special case of the other.
_ENQUEUE_INDEX_PULL_REQUEST_TRIGGERS: frozenset[tuple[str, str]] = frozenset(
    {
        ("pull_request", "opened"),
        ("pull_request", "synchronize"),
    }
)

# Per docs/plans/plan-deliver-memory.md's Phase 3 section (Unit C, Plan
# Assumption 3): `issue_comment` is deliberately never a member here —
# GitHub fires it for a PR's top-level conversation tab, never tied to a
# specific review/line comment this platform could correlate back to a
# `posted_finding_comment` row, so subscribing to it would only add unused
# webhook volume. `pull_request_review_comment` "created" (a reply) and
# `pull_request_review_thread` "resolved"/"unresolved" are the only shapes
# GitHub's own API actually supports correlating.
_MEMORY_CAPTURE_TRIGGERS: frozenset[tuple[str, str]] = frozenset(
    {
        ("pull_request_review_comment", "created"),
        ("pull_request_review_thread", "resolved"),
        ("pull_request_review_thread", "unresolved"),
    }
)

# Per docs/plans/plan-deliver-memory.md's Phase 4 section (Unit D,
# Assumption 4): a superset of `_ENQUEUE_REVIEW_TRIGGERS`, adding `"closed"`
# — US-M3's second trigger (a PR-closed event, independent of whether a
# `REVIEW` job is even enqueued for it). No new webhook event subscription
# needed for this addition: `pull_request` is already subscribed (Phase 4 of
# the walking-skeleton plan) for `REVIEW`'s own trigger set; this only adds
# one more *action* within that same already-subscribed event.
_MEMORY_REACTION_POLL_TRIGGERS: frozenset[tuple[str, str]] = _ENQUEUE_REVIEW_TRIGGERS | {
    ("pull_request", "closed")
}


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


def is_index_trigger(
    event_type: str,
    action: str | None,
    ref: str | None,
    default_branch: str | None,
) -> bool:
    """Whether this webhook event should enqueue an ``INDEX`` job, per the
    plan's Phase 1 section.

    True for a ``push`` event whose ``ref`` matches that same payload's
    ``repository.default_branch`` (US1) — ``ref``/``default_branch`` are
    both optional per ``adapter/github/schemas.py``'s own optionality, so
    either being ``None`` (a malformed/adversarial delivery, or simply a
    non-``push`` event this function wasn't asked about) resolves to "no
    match" rather than raising or guessing. True for a ``pull_request``
    event with action ``opened`` or ``synchronize`` (US2) — the same two
    actions ``is_enqueue_trigger`` already matches for ``REVIEW``, restated
    here as this function's own independent trigger table (see
    ``_ENQUEUE_INDEX_PULL_REQUEST_TRIGGERS``'s docstring for why it isn't
    just an alias). Every other ``event_type`` (``ping``,
    ``installation``, ...) is always "no match".

    Pure, no-I/O logic per ADR-0005, alongside ``is_enqueue_trigger`` in
    this same module — the caller
    (``apps/webhook_api/webhook_routes.py``) is responsible for also
    consulting the ``indexing_enabled`` kill switch before actually
    enqueuing anything; this function's own answer is unconditional on that
    switch by design, per architecture.md's "parse config once, pass down
    explicitly" rule (config never read from inside pure domain/adapter
    logic).
    """
    if event_type == "push":
        if ref is None or default_branch is None:
            return False
        return ref == f"refs/heads/{default_branch}"
    if event_type == "pull_request":
        if action is None:
            return False
        return (event_type, action) in _ENQUEUE_INDEX_PULL_REQUEST_TRIGGERS
    return False


def is_memory_capture_trigger(event_type: str, action: str | None) -> bool:
    """Whether this ``(event_type, action)`` pair should attempt to capture
    a reply/thread-resolution raw signal, per
    ``docs/plans/plan-deliver-memory.md``'s Phase 3 section (Unit C,
    US-M1/US-M2) — a third, independent trigger table alongside
    ``is_enqueue_trigger``/``is_index_trigger``, answering yet another
    different question ("attempt Memory capture", never "enqueue a job";
    no job is ever enqueued for a matching event here).

    ``action`` is ``None`` for event types that carry no top-level
    ``action`` key at all — always "no match" here, the same convention
    ``is_enqueue_trigger`` already establishes.

    Pure, no-I/O logic per ADR-0005, tested at Unit tier
    (``adapter/github/test_triggers.py``) alongside its two siblings.
    """
    if action is None:
        return False
    return (event_type, action) in _MEMORY_CAPTURE_TRIGGERS


def is_memory_reaction_poll_trigger(event_type: str, action: str | None) -> bool:
    """Whether this ``(event_type, action)`` pair should enqueue a
    ``MEMORY_REACTION_POLL`` job, per
    ``docs/plans/plan-deliver-memory.md``'s Phase 4 section (Unit D,
    US-M3, Assumption 4).

    True for every action ``is_enqueue_trigger`` already matches
    (``opened``/``synchronize``/``reopened`` — a later push or a fresh PR is
    an opportunity to observe reactions accumulated since the last poll)
    plus ``closed`` (US-M3's second `Given/When/Then` branch — reactions are
    polled once more before the PR's own comment thread goes quiet for
    good), independent of whether a `REVIEW`/`INDEX` job is enqueued for the
    same event.

    ``action`` is ``None`` for event types that carry no top-level
    ``action`` key at all — always "no match" here, the same convention
    ``is_enqueue_trigger``/``is_memory_capture_trigger`` already establish.

    Pure, no-I/O logic per ADR-0005, tested at Unit tier
    (``adapter/github/test_triggers.py``) alongside its three siblings.
    """
    if action is None:
        return False
    return (event_type, action) in _MEMORY_REACTION_POLL_TRIGGERS
