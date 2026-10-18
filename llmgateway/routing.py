"""llmgateway.routing — model-routing resolution (Unit F), per
``docs/plans/plan-deliver-admin-gui.md``'s Phase 5 section and Assumption 3.

``FallbackModelRoutingResolver`` is a **concrete class, not a Protocol** —
only one implementation (the org -> platform -> static-default fallback
chain) is in view, per ``docs/rules/architecture.md``'s "no interface
without a second real implementation already in view" rule, the same
reasoning already applied to ``adapter/storage/queue.py``'s
``JobQueueRepository``. It is imported directly by **both**
``apps/worker`` (to select the model at job-dispatch time) and
``apps/admin_api`` (to compute the same effective-model-plus-source for
``GET /api/v1/model-routing``) — one shared implementation, not two
independently-derived copies of the same fallback chain, per
architecture.md's "thin composition roots, not separate codebases with
duplicated logic" rule.

The resolver itself depends on a narrow ``ModelRoutingConfigReader``
Protocol (``get(org_id, stage) -> ModelRoutingConfigRow | None``), defined
here rather than at a consumer. This is **not** for swappability (there is
only one real implementation,
``adapter.storage.llm_routing.PostgresModelRoutingRepository``) — it exists
purely to preserve the domain/adapter dependency direction:
``llmgateway`` (a domain package) may never import
``adapter/storage``'s concrete repository directly, per architecture.md's
unconditional "the domain never imports entrypoint or adapter code" rule.
This mirrors Phases 4, 6, and 7's own consumer-defined Protocol for each of
their own adapter-satisfied dependencies.

**Fail-open, by design, not an oversight.** The reader's own Postgres read
is wrapped with an explicit, bounded timeout
(``asyncio.wait_for``, this codebase's existing timeout idiom — see
``review/context_builder.py``'s own ``internal_timeout_seconds`` use) and
**never lets a lookup failure propagate** — any exception raised by
``reader.get()``, and a timeout, both resolve to the static default model
instead. This is a deliberate, narrow exception to architecture.md's
"specific exception types... never swallow a domain error" rule, the same
kind of documented exception Phase 7's usage-counter write already takes
(see that phase's own "Observability & rollback" section): a job dispatch
must never hang or fail because Postgres is briefly unreachable, the same
"never fails closed if ``ModelRoutingConfig`` is empty" guarantee the spec
already requires for the missing-row case, extended here to the
unreachable-database case.

Zero infra imports beyond the OpenTelemetry *API* package, which
``docs/adr/0011-otel-api-imported-directly-in-domain-packages.md`` permits
directly in domain packages — mirrors ``review/llm_telemetry.py``'s own
module-level counter shape for the same reason (a stable, no-op-by-default,
vendor-neutral API, not a swappable infra dependency).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal, Protocol, runtime_checkable

from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

meter = metrics.get_meter("review.llm")

# `{source}` -> one of the four values `ModelRoutingSource` below names —
# a closed-set label value, never free text, matching this codebase's
# existing cardinality-bound convention (review/llm_telemetry.py's own
# `{stage, model, token_type}` counter). Gives operators a real signal for
# which tier is actually serving traffic, including the fail-open path,
# beyond a `DEBUG`-level log line that's typically disabled in production.
_model_routing_source_counter = meter.create_counter(
    "review.llm.model_routing_source_total",
    description="Count of model-routing resolutions, labeled by which tier served them.",
)

# The default per-call timeout for a `ModelRoutingConfigReader.get()` call —
# short enough that a Postgres blip degrades one job's model selection, not
# its dispatch latency; overridable per-instance for tests that need to
# force the timeout branch deterministically.
DEFAULT_MODEL_ROUTING_TIMEOUT_SECONDS = 2.0

ModelRoutingSource = Literal["org", "platform", "static_default", "fallback_on_error"]

# The four LLM-calling pipeline stages (per spec-admin-gui.md's US-A14,
# which named three; fix_generation was added by Phase 11) — a closed set, not an open string, so
# ``apps/admin_api/model_routing_routes.py`` can reject an unknown stage
# with a 422 (US-A15's write path) rather than silently accepting a typo
# that can never match a real step's own ``name``.
KNOWN_MODEL_ROUTING_STAGES: tuple[str, ...] = (
    "intent_summary",
    "review_generation",
    "verification",
    "fix_generation",
)


class ModelRoutingStoreError(Exception):
    """Raised by ``ModelRoutingConfigReader``/``PostgresModelRoutingRepository``
    methods on a real storage-layer failure (a malformed id string, a
    dropped connection mid-write, etc.) — never a raw
    ``sqlalchemy``/``asyncpg`` exception, per ``docs/rules/architecture.md``'s
    exception-type rule. Mirrors ``tenancy.settings.SettingStoreError``'s
    exact shape. ``FallbackModelRoutingResolver.resolve_with_source`` still
    catches this the same as any other reader failure (see this module's
    own "Fail-open, by design" docstring section) — this type only matters
    to a caller that isn't the resolver, e.g.
    ``apps/admin_api/model_routing_routes.py``'s write route (US-A15),
    which has no fallback chain of its own to lean on and must surface a
    real write failure to the caller.
    """

    def __init__(self, *, operation: str, detail: str) -> None:
        super().__init__(f"model routing store operation {operation!r} failed: {detail}")
        self.operation = operation
        self.detail = detail


class ModelRoutingConfigRow(BaseModel):
    """One ``ModelRoutingConfig`` row's domain-facing shape — ``org_id``
    ``None`` means the platform-wide default row, mirroring
    ``adapter/storage/models.py``'s own ``ModelRoutingConfig.org_id``
    column semantics.
    """

    model_config = ConfigDict(frozen=True)

    org_id: str | None
    stage: str
    model_name: str


@runtime_checkable
class ModelRoutingConfigReader(Protocol):
    """Narrow, domain-defined Protocol ``FallbackModelRoutingResolver``
    depends on — see this module's own docstring for why this exists
    despite only one implementation being in view.
    """

    async def get(self, *, org_id: str | None, stage: str) -> ModelRoutingConfigRow | None: ...


class ModelRoutingResolution(BaseModel):
    """``FallbackModelRoutingResolver.resolve_with_source``'s return
    shape — the effective model plus which tier actually served it, per
    US-A14's "effective model per stage + its source" requirement.
    """

    model_config = ConfigDict(frozen=True)

    model_name: str
    source: ModelRoutingSource


class FallbackModelRoutingResolver:
    """The org -> platform -> static-default fallback chain (US-A14,
    US-A15, US-A16) — see this module's own docstring for the full design.
    """

    def __init__(
        self,
        reader: ModelRoutingConfigReader,
        *,
        static_default_model: str,
        timeout_seconds: float = DEFAULT_MODEL_ROUTING_TIMEOUT_SECONDS,
    ) -> None:
        self._reader = reader
        self._static_default_model = static_default_model
        self._timeout_seconds = timeout_seconds

    async def resolve(self, stage: str, org_id: str | None) -> str:
        """The plain, model-name-only entry point every LLM-calling step
        (``IntentSummaryStep``/``ReviewGenerationStep``/``VerificationStep``)
        calls at dispatch time, per Assumption 3's exact signature. Thin
        wrapper over ``resolve_with_source`` — kept as two methods rather
        than one so a caller that only needs the model name (every Worker
        step) doesn't have to unpack a result it has no use for, while
        ``apps/admin_api``'s ``GET /api/v1/model-routing`` (which needs the
        source too) can call the richer method directly.
        """
        resolution = await self.resolve_with_source(stage, org_id)
        return resolution.model_name

    async def resolve_with_source(self, stage: str, org_id: str | None) -> ModelRoutingResolution:
        """The org -> platform -> static-default chain, plus which tier
        served it — used directly by ``apps/admin_api/model_routing_routes.py``'s
        ``GET /api/v1/model-routing`` (US-A14) and internally by
        ``resolve`` above.

        Fails open to the static default (source ``"fallback_on_error"``)
        on any reader error or on timeout — see this module's own
        docstring's "Fail-open, by design" section. Increments
        ``review.llm.model_routing_source_total{source}`` exactly once per
        call, regardless of which tier/branch served it.
        """
        try:
            model_name, source = await asyncio.wait_for(
                self._lookup(stage, org_id), timeout=self._timeout_seconds
            )
        except TimeoutError:
            logger.warning(
                "model routing config lookup timed out; falling open to the static default model",
                extra={
                    "stage": stage,
                    "org_id": org_id,
                    "timeout_seconds": self._timeout_seconds,
                },
            )
            model_name, source = self._static_default_model, "fallback_on_error"
        except Exception as exc:
            # Deliberately broad: this resolver must never let a storage-
            # layer failure (of any shape a `ModelRoutingConfigReader`
            # implementation might raise) hang or fail the calling job's
            # dispatch — see this module's own docstring's "Fail-open, by
            # design" section for the explicit justification this
            # otherwise-unconditional architecture.md deviation requires.
            logger.warning(
                "model routing config lookup failed; falling open to the static default model",
                extra={"stage": stage, "org_id": org_id, "error": str(exc)},
            )
            model_name, source = self._static_default_model, "fallback_on_error"

        _model_routing_source_counter.add(1, {"source": source})
        return ModelRoutingResolution(model_name=model_name, source=source)

    async def _lookup(self, stage: str, org_id: str | None) -> tuple[str, ModelRoutingSource]:
        """The unwrapped org -> platform -> static-default chain, run
        inside ``resolve_with_source``'s own ``asyncio.wait_for`` — kept as
        a separate coroutine so the whole three-tier lookup shares one
        timeout budget rather than one per reader call.
        """
        if org_id is not None:
            org_row = await self._reader.get(org_id=org_id, stage=stage)
            if org_row is not None:
                return org_row.model_name, "org"

        platform_row = await self._reader.get(org_id=None, stage=stage)
        if platform_row is not None:
            return platform_row.model_name, "platform"

        return self._static_default_model, "static_default"
