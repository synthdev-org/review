"""apps.admin_api.stats_service — PromQL query-building and derived-metric
math for ``GET /api/v1/stats`` (Unit G, US-A17), per
``docs/plans/plan-deliver-admin-gui.md``'s Phase 6 section and Assumption 4.

Kept as a separately unit-tested module, not inlined in
``apps/admin_api/stats_routes.py``'s route handler — checking
``apps/webhook_api/admin_routes.py`` during drafting (Assumption 4)
confirmed that file keeps its route handlers thin and delegates real logic
to domain packages (``memory.fingerprint``, ``indexing.embedding``); this
dashboard logic doesn't need a whole new bounded-context package (no
reusable domain rules, single consumer), but still needs a home outside
the route handler itself to stay unit-testable.

**``MetricsQuerier`` is defined here, at this module, rather than at a
domain package.** Per Assumption 4: ``apps/admin_api`` is this narrow,
one-method Protocol's one real consumer, and
``adapter.telemetry.prometheus_reader.PrometheusMetricsReader`` its one
real implementation — there is no bounded-context package this
single-consumer, no-reusable-domain-rules logic naturally belongs in, so
it follows ``docs/rules/architecture.md``'s ordinary "defined at the
consumer" rule directly, at this entrypoint module, rather than a domain
package. ``adapter/telemetry/prometheus_reader.py`` imports
``PrometheusSample`` from here — a deliberate, narrowly-scoped exception to
the usual adapter -> domain import direction (see that module's own
docstring) — but this module imports nothing from ``adapter`` in return:
the concrete reader is wired to a ``MetricsQuerier``-typed parameter only
at ``apps/admin_api/main.py``'s own composition root, per
``docs/rules/architecture.md``'s constructor-injection rule.

**Exact metric names, per Assumption 12** (verified to exist in the real
instrumented source tree, not assumed): ``review_queue_enqueued_total``,
``review_queue_claimed_total`` (``adapter/storage/queue.py``);
``review_llm_tokens_total{stage,model,token_type}``
(``review/llm_telemetry.py``); ``review_verification_judged_total``,
``review_verification_rejected_total``, ``review_verification_unmatched_total``,
unlabeled (``review/verification.py``); ``review_findings_count_total{severity,source}``
(``review/output.py``) — the underscore Prometheus-exporter form of each
dotted OTel name, per ``scripts/check-dashboard-metrics.py``'s own
documented naive ``.`` -> ``_`` translation (none of these five names
trips either of that script's two further documented normalizations: they
already end in a single ``_total``, and none is an ``_ms``-suffixed
histogram). "Queue depth" has no directly emitted gauge and is derived via
PromQL (``enqueued_total - claimed_total``), per Assumption 12.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

# The "recent" lookback window every rate-style query below uses — a plain
# module constant, not a new settings field: this phase's own plan section
# names exactly one new setting (AdminAPISettings.prometheus_base_url,
# Assumption 4), and a fixed, documented window keeps this dashboard's
# numbers stable/comparable across repeated loads rather than adding a
# second knob nothing in this plan's scope asks for. Returned verbatim as
# each windowed stat's own `window` field (US-A17's "each labeled with the
# time window it covers").
RECENT_WINDOW_LABEL = "1h"
_RECENT_WINDOW_PROMQL = "[1h]"

# The label US-A17's own Given/When/Then uses for queue depth: a
# point-in-time derived value (the difference of two cumulative counters'
# *current* totals), not a windowed rate — "current", not "1h", correctly
# describes what it measures.
CURRENT_WINDOW_LABEL = "current"


class PrometheusSample(BaseModel):
    """One label-set + numeric value from a Prometheus instant-query
    result's ``result`` array — ``MetricsQuerier.query``'s own return
    shape.
    """

    model_config = ConfigDict(frozen=True)

    labels: dict[str, str]
    value: float


@runtime_checkable
class MetricsQuerier(Protocol):
    """Narrow, one-method Protocol ``adapter.telemetry.prometheus_reader
    .PrometheusMetricsReader`` implements (Assumption 4) — an instant
    PromQL query, returning every label-set/value pair Prometheus's own
    ``/api/v1/query`` response carries for that expression, or an empty
    tuple for a genuinely empty result set (never an error for "no data
    yet").
    """

    async def query(self, promql: str) -> tuple[PrometheusSample, ...]: ...


# --- Response shapes -------------------------------------------------------


class QueueDepthStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    depth: int
    window: str


class LLMTokenUsageEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: str
    model: str
    token_type: str
    tokens: int


class LLMTokenUsageStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    entries: tuple[LLMTokenUsageEntry, ...]
    window: str


class VerificationFunnelStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    judged: int
    rejected: int
    unmatched: int
    window: str


class FindingsBySeverityEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    severity: str
    source: str
    count: int


class FindingsStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    entries: tuple[FindingsBySeverityEntry, ...]
    window: str


class AdminStatsView(BaseModel):
    """``GET /api/v1/stats``'s full response — US-A17's four named
    categories, each labeled with the time window it covers."""

    model_config = ConfigDict(frozen=True)

    queue_depth: QueueDepthStats
    llm_token_usage: LLMTokenUsageStats
    verification_funnel: VerificationFunnelStats
    findings_by_severity: FindingsStats


# --- Query building ---------------------------------------------------------


def build_queue_depth_query() -> str:
    """ "Queue depth" is not a directly emitted gauge (Assumption 12) —
    derived as the difference between the two cumulative counters' current
    totals, summed across every job type (this codebase's own "no counter
    distinct from the shared queue/job metrics was found for a separate
    index health signal" finding, Assumption 12 — queue depth already
    covers both REVIEW and INDEX jobs together)."""
    return "sum(review_queue_enqueued_total) - sum(review_queue_claimed_total)"


def build_llm_token_usage_query() -> str:
    """Recent LLM token usage, summed by ``{stage, model, token_type}`` —
    the same three attributes ``review/llm_telemetry.py`` records against,
    per US-A17's "LLM token spend" requirement."""
    return (
        "sum by (stage, model, token_type) "
        f"(increase(review_llm_tokens_total{_RECENT_WINDOW_PROMQL}))"
    )


def build_verification_funnel_queries() -> tuple[str, str, str]:
    """The three unlabeled verification-funnel counters (judged, rejected,
    unmatched) — three separate queries since each is a distinct metric
    name with no shared label to group by, per
    ``docs/specs/spec-observability.md``'s US5 counters (referenced by
    US-A17's own Given/When/Then)."""
    return (
        f"increase(review_verification_judged_total{_RECENT_WINDOW_PROMQL})",
        f"increase(review_verification_rejected_total{_RECENT_WINDOW_PROMQL})",
        f"increase(review_verification_unmatched_total{_RECENT_WINDOW_PROMQL})",
    )


def build_findings_by_severity_query() -> str:
    """Recent findings, grouped by ``{severity, source}`` — the same two
    attributes ``review/output.py`` records against, per US-A17's
    "findings-by-severity breakdown" requirement (``source`` is carried
    along since the underlying counter is already tagged by it, and
    dropping it would silently conflate LLM- and static-analyzer-sourced
    findings of the same severity)."""
    return (
        f"sum by (severity, source) (increase(review_findings_count_total{_RECENT_WINDOW_PROMQL}))"
    )


# --- Result parsing (derived-metric math) -----------------------------------


def _single_value(samples: tuple[PrometheusSample, ...]) -> float:
    """Every label-free (scalar-shaped) query above returns at most one
    sample (Prometheus's vector result for an aggregate expression with no
    remaining ``by (...)`` labels) — ``0.0`` for a genuinely empty result
    set (no data recorded yet), never an error."""
    return samples[0].value if samples else 0.0


def parse_queue_depth(samples: tuple[PrometheusSample, ...]) -> QueueDepthStats:
    return QueueDepthStats(depth=round(_single_value(samples)), window=CURRENT_WINDOW_LABEL)


def parse_llm_token_usage(samples: tuple[PrometheusSample, ...]) -> LLMTokenUsageStats:
    entries = tuple(
        LLMTokenUsageEntry(
            stage=sample.labels.get("stage", ""),
            model=sample.labels.get("model", ""),
            token_type=sample.labels.get("token_type", ""),
            tokens=round(sample.value),
        )
        for sample in samples
    )
    return LLMTokenUsageStats(entries=entries, window=RECENT_WINDOW_LABEL)


def parse_verification_funnel(
    judged_samples: tuple[PrometheusSample, ...],
    rejected_samples: tuple[PrometheusSample, ...],
    unmatched_samples: tuple[PrometheusSample, ...],
) -> VerificationFunnelStats:
    return VerificationFunnelStats(
        judged=round(_single_value(judged_samples)),
        rejected=round(_single_value(rejected_samples)),
        unmatched=round(_single_value(unmatched_samples)),
        window=RECENT_WINDOW_LABEL,
    )


def parse_findings_by_severity(samples: tuple[PrometheusSample, ...]) -> FindingsStats:
    entries = tuple(
        FindingsBySeverityEntry(
            severity=sample.labels.get("severity", ""),
            source=sample.labels.get("source", ""),
            count=round(sample.value),
        )
        for sample in samples
    )
    return FindingsStats(entries=entries, window=RECENT_WINDOW_LABEL)


# --- Orchestration -----------------------------------------------------------


async def get_admin_stats(querier: MetricsQuerier) -> AdminStatsView:
    """US-A17: runs every query above against ``querier`` and assembles the
    full ``GET /api/v1/stats`` response. A ``PrometheusUnreachableError``
    raised by any one ``query()`` call propagates unhandled — this function
    makes no attempt to return a partial result, since a caller (the route
    handler) needs one clear signal of whether the whole read succeeded, per
    this phase's own "clean 5xx, never a hang" Observability requirement.
    """
    queue_depth_samples = await querier.query(build_queue_depth_query())
    llm_token_usage_samples = await querier.query(build_llm_token_usage_query())
    judged_query, rejected_query, unmatched_query = build_verification_funnel_queries()
    judged_samples = await querier.query(judged_query)
    rejected_samples = await querier.query(rejected_query)
    unmatched_samples = await querier.query(unmatched_query)
    findings_samples = await querier.query(build_findings_by_severity_query())

    return AdminStatsView(
        queue_depth=parse_queue_depth(queue_depth_samples),
        llm_token_usage=parse_llm_token_usage(llm_token_usage_samples),
        verification_funnel=parse_verification_funnel(
            judged_samples, rejected_samples, unmatched_samples
        ),
        findings_by_severity=parse_findings_by_severity(findings_samples),
    )
