"""tenancy.usage — the free-tier usage counter's domain boundary, per
``docs/plans/plan-deliver-admin-gui.md``'s Phase 7 section (Unit H, US-A18,
US-A19, Assumption 2).

Per Assumption 2, this bounded-context extension (not a new package) is
where the usage counter's two Protocols live — ``tenancy`` already models
``Org``/``Repo``/``Team`` and (Phase 4) ``OrgRepoTeamSetting``'s own
reader/writer logic, so counting private-repo reviews per connected org is
a natural extension of that same bounded context, not a new one.
``UsageCounterRecorder``/``UsageCounterReader`` are defined here, at the
data model, per `ADR-0006
<../docs/adr/0006-protocol-defined-at-data-model.md>`_'s
multi-entrypoint-consumer exemption — ``apps/worker`` (the Worker records
on job completion) and ``apps/admin_api`` (the Admin API reads for the
dashboard) are two independent entrypoint consumers of the same storage
boundary, the same reasoning that already placed ``memory.store``'s
``MemorySignalRecorder`` at its data model instead of one consumer.

Zero infra imports, per ``docs/rules/architecture.md``'s domain-package
rule: everything below is Protocols and plain pydantic models — no
SQLAlchemy, no FastAPI, no I/O. ``adapter/storage/tenancy.py``'s
``PostgresUsageCounterRepository`` implements both Protocols; that same
module's own ``day_for`` pure function (not here — see its own docstring
for why it lives at the adapter instead, per this phase's own plan
section) computes the 04:00-anchored calendar day this package's
``UsageCountRow.day`` is always already expressed in.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

FREE_TIER_DAILY_REVIEW_LIMIT = 20
"""The free-tier guideline (ADR-0008's Decision: "free... up to 20 private
reviews/calendar-day... per connected GitHub org") — informational only,
per that ADR's "the software enforces nothing" framing; nothing in this
plan gates functionality on crossing it (US-A18's second Given/When/Then).
"""


class UsageCountRow(BaseModel):
    """One connected installation's current count for one 04:00-anchored
    day, plus (when resolvable) the org it belongs to — the shape
    ``apps/admin_api/usage_routes.py``'s ``GET /api/v1/usage`` (US-A18)
    renders one row per.

    ``org_id``/``org_name`` are best-effort: they're resolved by
    ``PostgresUsageCounterRepository`` via a join through ``Repo`` (the
    only table that actually carries ``installation_id`` today — ``Org``
    itself has no such column, see that repository's own docstring) and
    are ``None`` when no ``Repo`` row for this ``installation_id`` exists
    yet (a genuinely reachable case: a counter row can exist for an
    installation whose ``Repo`` row was since deleted, or — defensively —
    one this read races a write for). A missing org identity never
    excludes the row from the response; ``installation_id`` alone is
    always enough to identify *which* counter this is.
    """

    model_config = ConfigDict(frozen=True)

    installation_id: int
    org_id: str | None
    org_name: str | None
    day: date
    count: int

    @property
    def over_threshold(self) -> bool:
        """US-A18's second Given/When/Then: "an org's count exceeds 20 for
        the day... clearly marked as over the free-tier guideline."
        """
        return self.count > FREE_TIER_DAILY_REVIEW_LIMIT


class UsageCounterStoreError(Exception):
    """Raised by ``UsageCounterRecorder``/``UsageCounterReader`` methods on
    a real storage-layer failure — never a raw ``sqlalchemy``/``asyncpg``
    exception, per ``docs/rules/architecture.md``'s exception-type rule.
    Mirrors ``tenancy.settings.SettingStoreError``'s exact shape.

    ``apps/worker/dispatch.py``'s usage-counter increment call deliberately
    catches this (and any other exception `increment` might raise) and
    degrades to a logged, non-raised failure plus a
    ``review.usage_counter.increment_failed_total`` metric — see that
    module's own docstring for why this is a narrow, documented exception
    to "never swallow a domain error," not a precedent for swallowing
    errors elsewhere.
    """

    def __init__(self, *, operation: str, detail: str) -> None:
        super().__init__(f"usage counter store operation {operation!r} failed: {detail}")
        self.operation = operation
        self.detail = detail


class UsageCounterRecorder(Protocol):
    """Write boundary for the usage counter — implemented by
    ``adapter/storage/tenancy.py``'s ``PostgresUsageCounterRepository``.
    Consumed by ``apps/worker/dispatch.py`` on every successfully-completed
    ``REVIEW`` job for a private repo (US-A19).
    """

    async def increment(self, *, installation_id: int, now: datetime) -> None:
        """Atomically increments ``installation_id``'s counter for
        ``now``'s 04:00-anchored calendar day by one, creating the row on
        first increment for that ``(installation_id, day)`` pair.
        """
        ...


class UsageCounterReader(Protocol):
    """Read boundary for the usage counter — implemented by
    ``adapter/storage/tenancy.py``'s ``PostgresUsageCounterRepository``.
    Consumed by ``apps/admin_api/usage_routes.py`` (US-A18).
    """

    async def list_current_day_counts(self, *, now: datetime) -> tuple[UsageCountRow, ...]:
        """Every connected installation's count for ``now``'s
        04:00-anchored calendar day — installations with no reviews yet
        today simply have no row (and so are absent from this list), per
        US-A18's first Given/When/Then ("at least one org has performed
        private-repo reviews today").
        """
        ...
