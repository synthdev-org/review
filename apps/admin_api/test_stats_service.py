"""Unit tests for apps/admin_api/stats_service.py — PromQL query-building
and derived-metric math (Unit G, US-A17), against a fake ``MetricsQuerier``,
per docs/plans/plan-deliver-admin-gui.md's Phase 6 "Tests" section.

No real I/O, adapters, network, or database — docs/rules/testing.md's unit
tier.
"""

from __future__ import annotations

import pytest

from apps.admin_api.stats_service import (
    CURRENT_WINDOW_LABEL,
    RECENT_WINDOW_LABEL,
    MetricsQuerier,
    PrometheusSample,
    build_findings_by_severity_query,
    build_llm_token_usage_query,
    build_queue_depth_query,
    build_verification_funnel_queries,
    get_admin_stats,
    parse_findings_by_severity,
    parse_llm_token_usage,
    parse_queue_depth,
    parse_verification_funnel,
)


class _FakeMetricsQuerier:
    """Records every PromQL string it was asked to run, and returns a
    caller-supplied canned response keyed by that exact string — a fake,
    not a mock, per docs/rules/testing.md's "fakes over mocks for adapters"
    convention.
    """

    def __init__(self, responses: dict[str, tuple[PrometheusSample, ...]]) -> None:
        self._responses = responses
        self.queries_received: list[str] = []

    async def query(self, promql: str) -> tuple[PrometheusSample, ...]:
        self.queries_received.append(promql)
        return self._responses.get(promql, ())


def _assert_is_metrics_querier(querier: object) -> None:
    assert isinstance(querier, MetricsQuerier)


# --- Query building ----------------------------------------------------------


def test_queue_depth_query_subtracts_claimed_from_enqueued() -> None:
    query = build_queue_depth_query()
    assert query == "sum(review_queue_enqueued_total) - sum(review_queue_claimed_total)"


def test_llm_token_usage_query_groups_by_stage_model_token_type() -> None:
    query = build_llm_token_usage_query()
    assert "review_llm_tokens_total" in query
    assert "by (stage, model, token_type)" in query
    assert "[1h]" in query


def test_verification_funnel_queries_are_three_distinct_counters_summed_over_category() -> None:
    judged, rejected, unmatched = build_verification_funnel_queries()
    assert "review_verification_judged_total" in judged
    assert "review_verification_rejected_total" in rejected
    assert "review_verification_unmatched_total" in unmatched
    assert len({judged, rejected, unmatched}) == 3
    # The counters carry a `category` label; `_single_value` reads one sample,
    # so an un-summed query would silently report a single category's count.
    assert all(query.startswith("sum(") for query in (judged, rejected, unmatched))


def test_findings_by_severity_query_groups_by_severity_and_source() -> None:
    query = build_findings_by_severity_query()
    assert "review_findings_count_total" in query
    assert "by (severity, source)" in query


# --- Derived-metric math (parsing) -------------------------------------------


def test_parse_queue_depth_from_single_scalar_sample() -> None:
    stats = parse_queue_depth((PrometheusSample(labels={}, value=7.0),))
    assert stats.depth == 7
    assert stats.window == CURRENT_WINDOW_LABEL


def test_parse_queue_depth_with_no_samples_defaults_to_zero() -> None:
    """A brand-new deployment with no queue activity yet — an empty
    Prometheus result set, not an error (adapter.telemetry.prometheus_reader's
    own "empty tuple for genuinely empty data" contract)."""
    stats = parse_queue_depth(())
    assert stats.depth == 0


def test_parse_queue_depth_rounds_to_nearest_int() -> None:
    """Prometheus's `increase()`/subtraction of two counters can yield a
    non-integer float (e.g. a counter reset mid-window) — rounded, not
    truncated or left as a float, since a dashboard depth is inherently a
    whole-job count."""
    stats = parse_queue_depth((PrometheusSample(labels={}, value=6.6),))
    assert stats.depth == 7


def test_parse_llm_token_usage_maps_every_labeled_sample_to_an_entry() -> None:
    samples = (
        PrometheusSample(
            labels={"stage": "review_generation", "model": "local-model", "token_type": "prompt"},
            value=1200.0,
        ),
        PrometheusSample(
            labels={
                "stage": "review_generation",
                "model": "local-model",
                "token_type": "completion",
            },
            value=340.0,
        ),
    )
    stats = parse_llm_token_usage(samples)
    assert stats.window == RECENT_WINDOW_LABEL
    assert len(stats.entries) == 2
    by_type = {entry.token_type: entry.tokens for entry in stats.entries}
    assert by_type == {"prompt": 1200, "completion": 340}
    assert all(entry.stage == "review_generation" for entry in stats.entries)


def test_parse_verification_funnel_assembles_all_three_counters() -> None:
    stats = parse_verification_funnel(
        (PrometheusSample(labels={}, value=10.0),),
        (PrometheusSample(labels={}, value=2.0),),
        (PrometheusSample(labels={}, value=1.0),),
    )
    assert stats.judged == 10
    assert stats.rejected == 2
    assert stats.unmatched == 1
    assert stats.window == RECENT_WINDOW_LABEL


def test_parse_verification_funnel_with_no_data_defaults_every_count_to_zero() -> None:
    stats = parse_verification_funnel((), (), ())
    assert (stats.judged, stats.rejected, stats.unmatched) == (0, 0, 0)


def test_parse_findings_by_severity_maps_every_labeled_sample_to_an_entry() -> None:
    samples = (
        PrometheusSample(labels={"severity": "high", "source": "llm"}, value=3.0),
        PrometheusSample(labels={"severity": "low", "source": "static_analysis"}, value=8.0),
    )
    stats = parse_findings_by_severity(samples)
    assert stats.window == RECENT_WINDOW_LABEL
    entries_by_severity = {entry.severity: (entry.source, entry.count) for entry in stats.entries}
    assert entries_by_severity == {
        "high": ("llm", 3),
        "low": ("static_analysis", 8),
    }


# --- Orchestration -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_admin_stats_issues_every_query_and_assembles_the_full_view() -> None:
    queue_query = build_queue_depth_query()
    llm_query = build_llm_token_usage_query()
    judged_query, rejected_query, unmatched_query = build_verification_funnel_queries()
    findings_query = build_findings_by_severity_query()

    fake = _FakeMetricsQuerier(
        {
            queue_query: (PrometheusSample(labels={}, value=5.0),),
            llm_query: (
                PrometheusSample(
                    labels={"stage": "verification", "model": "local-model", "token_type": "total"},
                    value=42.0,
                ),
            ),
            judged_query: (PrometheusSample(labels={}, value=9.0),),
            rejected_query: (PrometheusSample(labels={}, value=1.0),),
            unmatched_query: (PrometheusSample(labels={}, value=0.0),),
            findings_query: (
                PrometheusSample(labels={"severity": "medium", "source": "llm"}, value=2.0),
            ),
        }
    )

    view = await get_admin_stats(fake)

    assert view.queue_depth.depth == 5
    assert view.llm_token_usage.entries[0].tokens == 42
    assert view.verification_funnel.judged == 9
    assert view.verification_funnel.rejected == 1
    assert view.verification_funnel.unmatched == 0
    assert view.findings_by_severity.entries[0].severity == "medium"
    # Every one of the four category's own query was actually issued — not
    # a stale/cached/partial read.
    assert set(fake.queries_received) == {
        queue_query,
        llm_query,
        judged_query,
        rejected_query,
        unmatched_query,
        findings_query,
    }


@pytest.mark.asyncio
async def test_get_admin_stats_with_a_fresh_platform_returns_all_zeros_not_an_error() -> None:
    """US-A17's own Given/When/Then assumes at least one job has been
    processed, but a genuinely empty platform (fresh deploy, dashboard
    opened before any job runs) must still render zeros, never fail."""
    fake = _FakeMetricsQuerier({})

    view = await get_admin_stats(fake)

    assert view.queue_depth.depth == 0
    assert view.llm_token_usage.entries == ()
    assert (view.verification_funnel.judged, view.verification_funnel.rejected) == (0, 0)
    assert view.findings_by_severity.entries == ()


def test_metrics_querier_protocol_is_runtime_checkable_against_the_fake() -> None:
    _assert_is_metrics_querier(_FakeMetricsQuerier({}))
