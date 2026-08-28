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


class GitHubWebhookEnvelope(BaseModel):
    """Minimal envelope used to resolve the installation ID a webhook event
    belongs to.

    Per the plan's Phase 4 section: "Installation-ID resolution at this
    phase is payload parsing only — no DB lookup, no persisted mapping."
    This model validates just enough of GitHub's webhook payload shape to
    extract that ID; every other event-specific field (there are dozens,
    varying per event type) is ignored via ``extra="ignore"`` rather than
    modeled here, since nothing downstream in this phase consumes them yet.
    """

    model_config = ConfigDict(extra="ignore")

    installation: Installation


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
