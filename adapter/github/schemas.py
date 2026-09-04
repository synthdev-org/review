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


class PullRequestHead(BaseModel):
    """The ``pull_request.head`` object — only ``sha`` is needed here."""

    model_config = ConfigDict(extra="ignore")

    # Optional per the plan's Phase 1 section: a malformed/adversarial
    # delivery missing this field must be accepted-but-not-enqueued, never
    # rejected with a 4xx at the schema layer — see
    # apps/webhook_api/webhook_routes.py's point-of-use validation.
    sha: str | None = None


class PullRequest(BaseModel):
    """The ``pull_request`` object present on every real ``pull_request``
    event payload — absent entirely on other event types (e.g. ``push``)."""

    model_config = ConfigDict(extra="ignore")

    # Both optional for the same reason as PullRequestHead.sha above.
    number: int | None = None
    head: PullRequestHead | None = None


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
