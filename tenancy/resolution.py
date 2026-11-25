"""tenancy.resolution — installation/org/repo resolution and webhook
delivery idempotency: the domain rules answering "what does it mean to see
this installation for the first time" and "has this delivery already been
processed."

Per the plan's Phase 1 section ("Correction to an over-hasty first draft of
this bullet (architecture boundary)"): these are domain rules, not
entrypoint plumbing, so they live here (``tenancy``, already scaffolded
with exactly this docstring) rather than inline in
``apps/webhook_api/webhook_routes.py`` — organizing by bounded context, not
layer, per docs/rules/architecture.md.

``TenancyStore`` is a ``typing.Protocol`` defined here, at its consumer
(``resolve_installation`` below), per architecture.md's "interfaces are
typing.Protocols ... defined at the consumer" rule;
``adapter/storage/tenancy.py``'s ``PostgresTenancyStore`` implements it.
``unit_of_work`` is typed ``object`` rather than
``sqlalchemy.ext.asyncio.AsyncSession`` so this module stays free of that
infra import — ``resolve_installation`` never inspects the value, only
relays it through to ``store`` unchanged; see the plan's "Transaction
boundary" bullet for how the concrete adapter narrows it back to a real
session.

``WebhookEvent`` is a domain-owned input model, not
``adapter/github/schemas.py``'s ``GitHubWebhookEnvelope`` — so this package
never has to import from ``adapter/github`` to type-check under
``mypy --strict``, and a future second git-host adapter can call into this
same resolution logic via its own translation to ``WebhookEvent`` rather
than a second, near-duplicate resolution function.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class WebhookEvent(BaseModel):
    """A git-host-neutral view of one webhook delivery.

    ``apps/webhook_api/webhook_routes.py`` builds this from the parsed
    ``GitHubWebhookEnvelope`` and the request's ``X-GitHub-Delivery``
    header immediately before calling ``resolve_installation`` — the
    adapter-to-domain translation happens once, at that boundary, not
    inside this package.

    ``action``/``pr_number``/``head_sha``/``base_sha`` are optional: a real
    GitHub ``push`` delivery has no top-level ``action`` key and no
    ``pull_request`` object at all, and a required field here would reject
    every such delivery before ``adapter/github/triggers.py``'s lookup ever
    runs. ``base_sha`` (per
    docs/plans/plan-deliver-context-builder.md's Phase 1 section) is the
    PR's base-commit SHA, threaded through for the Context Builder's
    (Phase 5) pre-change content fetch — optional for the same reason
    ``head_sha`` already is: GitHub's payload can omit it, and this is a
    genuine, benign production case (Phase 5 skips the pre-change fetch
    entirely when it's ``None``), not only a deploy-migration-window
    concern.

    ``repo_private`` is whether GitHub reports this repo as
    private — sourced from ``adapter/github/schemas.py``'s
    ``Repository.private`` (see that field's own docstring for why it
    defaults to ``False`` rather than being required), threaded through by
    ``apps/webhook_api/webhook_routes.py``'s ``_build_webhook_event`` into
    the ``REVIEW`` job payload's own ``"private"`` field.
    """

    model_config = ConfigDict(frozen=True)

    delivery_id: str
    installation_id: int
    action: str | None
    repo_full_name: str
    pr_number: int | None
    head_sha: str | None
    base_sha: str | None = None
    repo_private: bool = False
    # docs/plans/plan-deliver-product-knowledge.md Phase 2 (US-PK2 capture,
    # A1). ``pr_issue_keys`` are extracted at the webhook boundary
    # (``ingestion.pr_metadata``); the PR body is never carried.
    # ``pr_is_fork`` is ``None`` when the delivery is not a PR event; the
    # fail-closed derivation lives in ``ingestion.pr_metadata.is_fork_pr``.
    pr_title: str | None = None
    pr_head_ref: str | None = None
    pr_issue_keys: tuple[str, ...] = ()
    pr_is_fork: bool | None = None


class OrgRepoIds(BaseModel):
    """The org/repo identifiers ``TenancyStore.get_or_create_org_repo``
    resolves a webhook event's installation/repo to."""

    model_config = ConfigDict(frozen=True)

    org_id: uuid.UUID
    repo_id: uuid.UUID


class ResolutionResult(BaseModel):
    """What ``resolve_installation`` hands back: the resolved org/repo, and
    whether this delivery id had been seen before."""

    model_config = ConfigDict(frozen=True)

    org_id: uuid.UUID
    repo_id: uuid.UUID
    is_new_delivery: bool


class TenancyStore(Protocol):
    """Storage boundary ``resolve_installation`` depends on — implemented by
    ``adapter/storage/tenancy.py``'s ``PostgresTenancyStore``, per
    architecture.md's Protocol-boundary rule ("accept protocols, return
    concrete types" defined at the consumer).
    """

    async def get_or_create_org_repo(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        unit_of_work: object | None = None,
    ) -> OrgRepoIds:
        """Return the existing Org/Repo for this installation/repo, or
        auto-provision both on first sight."""
        ...

    async def record_delivery_if_new(
        self, delivery_id: str, *, unit_of_work: object | None = None
    ) -> bool:
        """Record ``delivery_id`` as seen.

        Returns ``True`` the first time a given ``delivery_id`` is recorded
        (a new row was inserted), ``False`` if it had already been recorded
        (a genuine redelivery) — never raises for the duplicate case, per
        the plan's Phase 1 section: a redelivery is a no-op insert, not an
        error condition.
        """
        ...


async def resolve_installation(
    event: WebhookEvent, store: TenancyStore, *, unit_of_work: object | None = None
) -> ResolutionResult:
    """Auto-provision (or resolve) the Org/Repo ``event`` belongs to, and
    record its delivery id for idempotency, in that order.

    Runs for every accepted webhook regardless of trigger-set membership —
    see the plan's Phase 1 section, "This step runs for every accepted
    webhook ... the anti-replay/idempotency guarantee has to hold for all
    traffic, not only trigger-matched ``pull_request`` events."
    """
    org_repo = await store.get_or_create_org_repo(
        installation_id=event.installation_id,
        repo_full_name=event.repo_full_name,
        unit_of_work=unit_of_work,
    )
    is_new_delivery = await store.record_delivery_if_new(
        event.delivery_id, unit_of_work=unit_of_work
    )
    return ResolutionResult(
        org_id=org_repo.org_id,
        repo_id=org_repo.repo_id,
        is_new_delivery=is_new_delivery,
    )


def split_owner_repo(repo_full_name: str) -> tuple[str, str]:
    """Split a ``"owner/repo"``-shaped ``repo_full_name`` (this domain's own
    ``WebhookEvent.repo_full_name``/``TenancyStore.get_or_create_org_repo``
    shape, not a GitHub-specific one) into ``(owner, repo)``, raising a
    plain ``ValueError`` — never an unhandled tuple-unpacking error — when
    it doesn't contain the expected separator.

    Lives here, not in ``adapter/github/http.py``, even though every
    current caller of this parsing happens to be GitHub-adapter code today:
    per this module's own docstring, ``repo_full_name`` is a git-host-
    neutral concept ``resolve_installation``/``TenancyStore`` already
    depend on, and ``adapter/storage/tenancy.py`` (a *different* adapter
    package) needs this same split to provision Org/Repo rows. An adapter
    importing another adapter's internals is exactly the "if a new adapter
    requires a change to consumer code, the interface boundary is wrong"
    case architecture.md's Package structure section warns about; both
    ``adapter/github`` and ``adapter/storage`` depending inward on this
    domain module instead keeps that boundary honest, and a second
    git-host adapter gets the same parsing for free.
    """
    parts = repo_full_name.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"repo_full_name {repo_full_name!r} is not in 'owner/repo' form")
    return parts[0], parts[1]
