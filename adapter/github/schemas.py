"""adapter.github.schemas — pydantic models for data crossing the GitHub
webhook/App boundary, per docs/rules/architecture.md's "validate every
boundary ... with pydantic models — never trust a raw dict past the
adapter that received it" rule.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr


class Installation(BaseModel):
    """The ``installation`` object GitHub embeds in every webhook event
    payload for an App installed on the relevant repo/org."""

    id: int


class Repository(BaseModel):
    """The ``repository`` object GitHub embeds in every webhook event
    payload scoped to a repo (every event type Phase 1 subscribes to —
    ``pull_request`` and ``push`` — always carries one)."""

    model_config = ConfigDict(extra="ignore")

    full_name: str
    # Optional per the same "model only what's consumed, never reject a
    # well-formed delivery missing it" discipline as every other Phase 1
    # addition below: `adapter/github/triggers.py`'s `is_index_trigger`
    # compares a `push` payload's `ref` against this field to decide
    # default-branch-ness (US1); a `pull_request` delivery still carries a
    # `repository` object, but this plan's indexing trigger never consults
    # `default_branch` for that event type, so it stays optional rather than
    # required.
    default_branch: str | None = None
    # Phase 7 addition (docs/plans/plan-deliver-admin-gui.md's Phase 7
    # section, Unit H, US-A19) — GitHub's real `repository` object always
    # carries this field, but it defaults to `False` (never required) so
    # every pre-Phase-7 test fixture in this codebase that constructs a
    # `Repository` without it (verified by grep: none currently sets this
    # key) keeps parsing exactly as before, the same "model only what's
    # consumed... never reject a well-formed delivery missing it"
    # discipline `default_branch` above already establishes. Threaded
    # through `tenancy.resolution.WebhookEvent.repo_private` and into the
    # `REVIEW` job payload's own `"private"` field
    # (`apps/webhook_api/webhook_routes.py`'s `_base_job_identity_fields`)
    # so `apps/worker/dispatch.py` can decide whether a completed `REVIEW`
    # job counts toward the free-tier usage counter (US-A19's "a REVIEW job
    # for a public repository... no counter is incremented" clause) without
    # a second GitHub API round-trip at job-completion time.
    private: bool = False


class PullRequestHeadRepo(BaseModel):
    """
    The ``pull_request.head.repo`` object, reduced to what the fork check needs.

    Only what the product-knowledge exposure gate's fork check
    (docs/plans/plan-deliver-product-knowledge.md Phase 2, A1) needs. GitHub
    sends ``null`` here for a deleted fork.
    """

    model_config = ConfigDict(extra="ignore")

    full_name: str | None = None
    fork: bool | None = None


class PullRequestHead(BaseModel):
    """
    The ``pull_request.head`` object.

    ``sha`` plus the optional ``ref``/``repo`` the product-knowledge capture
    (Phase 2) reads.
    """

    model_config = ConfigDict(extra="ignore")

    # Optional per the plan's Phase 1 section: a malformed/adversarial
    # delivery missing this field must be accepted-but-not-enqueued, never
    # rejected with a 4xx at the schema layer — see
    # apps/webhook_api/webhook_routes.py's point-of-use validation.
    sha: str | None = None
    # Product-knowledge Phase 2 additions: same accepted-but-degraded
    # discipline as ``sha`` (never a 4xx on absence).
    ref: str | None = None
    repo: PullRequestHeadRepo | None = None


class PullRequestBase(BaseModel):
    """The ``pull_request.base`` object — only ``sha`` is needed here,
    mirroring ``PullRequestHead`` exactly. Per
    docs/plans/plan-deliver-context-builder.md's Phase 1 section: the PR's
    base-commit SHA the Context Builder (Phase 5) fetches pre-change content
    at, threaded through from here."""

    model_config = ConfigDict(extra="ignore")

    # Optional for the same reason as PullRequestHead.sha above: a
    # malformed/adversarial delivery missing this field must be
    # accepted-but-degraded (Phase 1's `base_sha is None` handling), never
    # rejected with a 4xx at the schema layer.
    sha: str | None = None
    # Product-knowledge Phase 2 (A1 fork check): the base repo's full name.
    repo: PullRequestHeadRepo | None = None


class PullRequest(BaseModel):
    """The ``pull_request`` object present on every real ``pull_request``
    event payload — absent entirely on other event types (e.g. ``push``)."""

    model_config = ConfigDict(extra="ignore")

    # Both optional for the same reason as PullRequestHead.sha above.
    number: int | None = None
    head: PullRequestHead | None = None
    base: PullRequestBase | None = None
    # Product-knowledge Phase 2 additions (F5). ``body`` is read only by
    # ``ingestion.pr_metadata.extract_issue_keys`` at the webhook boundary and
    # is never persisted or forwarded.
    title: str | None = None
    body: str | None = None


class PushCommit(BaseModel):
    """One entry of a ``push`` event payload's top-level ``commits`` array
    (and the shape of ``head_commit`` too, which is the same object).

    Per the plan's Phase 1 section, only the three file-path lists are
    modeled — every other per-commit field (author, message, timestamp,
    URL, ...) is ignored via ``extra="ignore"`` since nothing downstream
    consumes it yet.
    """

    model_config = ConfigDict(extra="ignore")

    added: list[str] = []
    removed: list[str] = []
    modified: list[str] = []


class GitHubWebhookEnvelope(BaseModel):
    """Minimal envelope used to resolve a webhook event's installation,
    repository, and (for ``pull_request`` events) PR identity.

    Per the plan's Phase 4 section: "Installation-ID resolution at this
    phase is payload parsing only — no DB lookup, no persisted mapping."
    Phase 1 extends this with ``repository.full_name`` (present on every
    event type this App subscribes to) and the optional ``action``/
    ``pull_request.number``/``pull_request.head.sha`` fields Phase 1's
    enqueue payload needs — every other event-specific field (there are
    dozens more, varying per event type) is still ignored via
    ``extra="ignore"`` rather than modeled here, since nothing downstream
    consumes them yet.

    ``action``/``pull_request`` are optional because a real GitHub ``push``
    delivery has no top-level ``action`` key and no ``pull_request`` object
    at all — a required field here would reject every real ``push``
    delivery with a 422 before ``adapter/github/triggers.py``'s lookup ever
    runs, contradicting this phase's "every non-enqueuing outcome is still
    accepted (200)" design.
    """

    model_config = ConfigDict(extra="ignore")

    installation: Installation
    # Optional: GitHub always delivers `ping` (on webhook creation) and the
    # `installation`/`installation_repositories`/`github_app_authorization`
    # family (App lifecycle changes) to this endpoint regardless of which
    # events the App subscribes to, and none of those payloads carries a
    # top-level `repository` key — a required field here would reject every
    # one of those well-formed, correctly-signed deliveries with a 422,
    # contradicting this module's own "every non-enqueuing outcome is still
    # accepted (200)" design. See apps/webhook_api/webhook_routes.py's
    # early return for events with no `repository`.
    repository: Repository | None = None
    action: str | None = None
    pull_request: PullRequest | None = None
    # Push-only fields (Phase 1's `INDEX` trigger, per the plan's Phase 1
    # section). Every one of these is optional, deliberately: a
    # `pull_request`, `ping`, or `installation` delivery carries none of
    # them, and a required field here would reject those already-working,
    # correctly-signed deliveries with a 422 — exactly the failure mode
    # `action`/`pull_request`'s own optionality above already avoids. Same
    # "model only what's consumed, ignore the rest via extra='ignore'"
    # discipline the envelope already follows throughout.
    ref: str | None = None
    before: str | None = None
    after: str | None = None
    commits: list[PushCommit] = []
    head_commit: PushCommit | None = None
    forced: bool | None = None
    # A branch/tag *deletion* push: GitHub sets this `true` and `after` to
    # the all-zeros SHA (a string, not absent) — `after is None` alone
    # never catches this case, so callers that must skip a deletion push
    # (e.g. `apps/webhook_api/webhook_routes.py`'s `INDEX` job enqueue,
    # which would otherwise try to index a commit that never existed)
    # check this field explicitly.
    deleted: bool | None = None


class PullRequestReviewComment(BaseModel):
    """The ``comment`` object on a ``pull_request_review_comment`` webhook
    event payload — per ``docs/plans/plan-deliver-memory.md``'s Phase 3
    section (Unit C). Only the three fields Phase 3's capture handler
    actually reads are modeled, per this module's own "model only what's
    consumed" discipline: ``id`` (correlated back to a captured reply's own
    ``memory_entry.github_comment_id``), ``in_reply_to_id`` (correlated
    against a ``posted_finding_comment`` row — the platform's own comment
    this is a reply to; ``None`` for a fresh top-level review comment, per
    the plan's Assumption 3), and ``body`` (redacted before persistence).
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    in_reply_to_id: int | None = None
    body: str


class PullRequestReviewCommentWebhookEnvelope(BaseModel):
    """A ``pull_request_review_comment`` webhook event payload, per the
    plan's Phase 3 section. ``repository``/``pull_request`` are modeled for
    payload-shape completeness (every real delivery of this event type
    carries both) even though this phase's own capture handler sources its
    ``repo_id``/``pr_number`` from the shared
    ``GitHubWebhookEnvelope``/``WebhookEvent`` parse that already ran at
    this route's boundary before this narrower envelope is parsed a second
    time — see ``apps/webhook_api/webhook_routes.py``'s ``_handle_memory_capture``.
    """

    model_config = ConfigDict(extra="ignore")

    action: str
    comment: PullRequestReviewComment
    repository: Repository
    pull_request: PullRequest


class ReviewThreadComment(BaseModel):
    """One entry of a ``pull_request_review_thread`` event payload's
    ``thread.comments`` array — only ``id`` is needed (Phase 3's own
    correlation lookup against ``posted_finding_comment``)."""

    model_config = ConfigDict(extra="ignore")

    id: int


class PullRequestReviewThread(BaseModel):
    """The ``thread`` object on a ``pull_request_review_thread`` webhook
    event payload."""

    model_config = ConfigDict(extra="ignore")

    comments: list[ReviewThreadComment] = []


class PullRequestReviewThreadWebhookEnvelope(BaseModel):
    """A ``pull_request_review_thread`` webhook event payload (action
    ``resolved``/``unresolved``), per the plan's Phase 3 section. See
    ``PullRequestReviewCommentWebhookEnvelope``'s own docstring for why
    ``repository``/``pull_request`` are modeled but not separately consumed
    here.
    """

    model_config = ConfigDict(extra="ignore")

    action: str
    thread: PullRequestReviewThread
    repository: Repository
    pull_request: PullRequest


class GitHubAppCredentials(BaseModel):
    """Credentials GitHub returns after exchanging a manifest-flow
    temporary code, per the plan's Phase 4 manifest/callback section.

    Handed back to the operator for their own bootstrap config (local
    ``.env`` or Helm ``Secret``/``existingSecret``) — never persisted to
    Postgres, per the plan's Compatibility & rollout section. The
    secret-valued fields are ``SecretStr`` so an accidental
    ``repr()``/log-line inclusion renders as a masked placeholder rather
    than the real value, per architecture.md's credential-handling rule;
    the one legitimate place these are unmasked is the JSON response built
    in ``apps/webhook_api/manifest_routes.py``, which is the intended
    delivery channel to the operator.
    """

    model_config = ConfigDict(frozen=True)

    app_id: int
    app_slug: str
    private_key: SecretStr
    webhook_secret: SecretStr
    client_id: str
    client_secret: SecretStr
