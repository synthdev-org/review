"""adapter.github.triggers — declarative mapping of GitHub webhook
``(event_type, action)`` pairs to "this should enqueue a review". Also home
to ``is_index_trigger``, the equivalent decision for "this should enqueue an
``INDEX`` job"; ``is_memory_capture_trigger``, the equivalent decision for
"this should attempt to capture a reply/thread-resolution raw signal"; and
``is_memory_reaction_poll_trigger``, the equivalent decision for "this
should enqueue a ``MEMORY_REACTION_POLL`` job" — four independent pure functions, not one
folded into another, since each answers a genuinely different question
with its own trigger table (see each function's own docstring).

Deliberately a plain ``frozenset[tuple[str, str]]`` membership check, not a
``dict`` keyed to a richer decision type — every populated entry today
resolves to the identical outcome (enqueue a review), so a dict buys
nothing over a set. If a future trigger needs a
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

# `pull_request` `INDEX`-triggering actions
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

# `issue_comment` is deliberately never a member here —
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

# A superset of `_ENQUEUE_REVIEW_TRIGGERS`, adding `"closed"`
# — US-M3's second trigger (a PR-closed event, independent of whether a
# `REVIEW` job is even enqueued for it). No new webhook event subscription
# needed for this addition: `pull_request` is already subscribed
# for `REVIEW`'s own trigger set; this only adds
# one more *action* within that same already-subscribed event.
_MEMORY_REACTION_POLL_TRIGGERS: frozenset[tuple[str, str]] = _ENQUEUE_REVIEW_TRIGGERS | {
    ("pull_request", "closed")
}


def _matches(event_type: str, action: str | None, table: frozenset[tuple[str, str]]) -> bool:
    """Shared ``action is None -> no match`` / ``(event_type, action) in
    table`` body every single-table trigger function below (each answering
    its own independent question against its own trigger table) otherwise
    repeats verbatim. ``is_index_trigger`` only routes its ``pull_request``
    branch through this — its ``push`` branch is a structurally different
    check (comparing ``ref`` to ``default_branch``, no table at all), so
    the function as a whole still isn't a single flat membership check.
    """
    if action is None:
        return False
    return (event_type, action) in table


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
    return _matches(event_type, action, _ENQUEUE_REVIEW_TRIGGERS)


def is_index_trigger(
    event_type: str,
    action: str | None,
    ref: str | None,
    default_branch: str | None,
) -> bool:
    """Whether this webhook event should enqueue an ``INDEX`` job.

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
        return _matches(event_type, action, _ENQUEUE_INDEX_PULL_REQUEST_TRIGGERS)
    return False


def is_memory_capture_trigger(event_type: str, action: str | None) -> bool:
    """Whether this ``(event_type, action)`` pair should attempt to capture
    a reply/thread-resolution raw signal
    (US-M1/US-M2) — a third, independent trigger table alongside
    ``is_enqueue_trigger``/``is_index_trigger``, answering yet another
    different question ("attempt Memory capture", never "enqueue a job";
    no job is ever enqueued for a matching event here).

    ``action`` is ``None`` for event types that carry no top-level
    ``action`` key at all — always "no match" here, the same convention
    ``is_enqueue_trigger`` already establishes.

    Pure, no-I/O logic per ADR-0005, tested at Unit tier
    (``adapter/github/test_triggers.py``) alongside its two siblings.
    """
    return _matches(event_type, action, _MEMORY_CAPTURE_TRIGGERS)


def is_memory_reaction_poll_trigger(event_type: str, action: str | None) -> bool:
    """Whether this ``(event_type, action)`` pair should enqueue a
    ``MEMORY_REACTION_POLL`` job (US-M3).

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
    return _matches(event_type, action, _MEMORY_REACTION_POLL_TRIGGERS)
