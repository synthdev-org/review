"""Unit tests for scripts/check-dashboard-metrics.py (US13's drift guard).

The guard script's filename uses a hyphen (matching this repo's `scripts/`
naming convention, e.g. `scripts/run-mutation-tests.sh`), which is not a
valid Python module name — it's loaded here via `importlib.util` rather
than a normal `import` statement.

Covers both the passing case (every dashboard-referenced metric name
resolves to a registered one) and the drift-detected case (a dashboard
references a metric name absent from the registered set), per
docs/specs/spec-observability.md. No real network/
DB I/O — only reading synthetic files this test itself writes to
`tmp_path`, matching the guard script's own "no I/O beyond reading the
dashboard JSON and grepping the source tree" scope.
"""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent / "check-dashboard-metrics.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("check_dashboard_metrics", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_dashboard_metrics = _load_module()


def _write_source_tree(repo_root: Path, instrument_calls: list[str]) -> None:
    """Writes a minimal `review/` package under `repo_root` whose single
    module contains the given `.create_*(...)` call source snippets, plus
    an excluded `test_*.py` sibling (to prove test files are never counted
    as "registered")."""
    review_dir = repo_root / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    body = "\n".join(instrument_calls)
    (review_dir / "instrumented.py").write_text(
        f"from opentelemetry import metrics\n\nmeter = metrics.get_meter(__name__)\n\n{body}\n",
        encoding="utf-8",
    )
    # A metric name that only appears in a test file must NOT be counted as
    # registered.
    (review_dir / "test_instrumented.py").write_text(
        'meter.create_counter("review.not_real.from_test_file_total")\n',
        encoding="utf-8",
    )


def _write_dashboard(dashboard_path: Path, exprs: list[str]) -> None:
    dashboard_path.parent.mkdir(parents=True, exist_ok=True)
    panels = [
        {"id": i, "type": "timeseries", "targets": [{"expr": expr}]} for i, expr in enumerate(exprs)
    ]
    dashboard_path.write_text(json.dumps({"panels": panels}), encoding="utf-8")


class TestRegisteredMetricNames:
    def test_finds_counter_and_histogram_names_and_excludes_test_files(
        self, tmp_path: Path
    ) -> None:
        _write_source_tree(
            tmp_path,
            [
                'meter.create_counter("review.queue.enqueued_total", description="x")',
                (
                    "meter.create_histogram(\n"
                    '    "review.pipeline.step.duration_ms",\n'
                    '    unit="ms",\n'
                    ")"
                ),
            ],
        )

        names = check_dashboard_metrics.registered_metric_names(tmp_path, ("review",))

        assert names == {"review.queue.enqueued_total", "review.pipeline.step.duration_ms"}

    def test_missing_search_root_is_skipped_not_an_error(self, tmp_path: Path) -> None:
        # No "adapter" directory exists under tmp_path at all.
        names = check_dashboard_metrics.registered_metric_names(tmp_path, ("adapter",))

        assert names == set()


class TestExpectedPrometheusNames:
    def test_dots_become_underscores_existing_underscores_survive(self) -> None:
        mapping = check_dashboard_metrics.expected_prometheus_names({"review.queue.enqueued_total"})

        assert mapping == {"review_queue_enqueued_total": "review.queue.enqueued_total"}

    def test_ms_suffixed_histogram_yields_only_the_milliseconds_suffixed_form(self) -> None:
        """Case 2 from this module's own docstring — discovered against a
        real Collector/Prometheus stack by tests/e2e/test_observability.py:
        a histogram whose name already ends in the short unit code ``_ms``
        is actually exported ONLY under the exporter's own full-word unit
        suffix — the naive, un-normalized form is never exported and must
        NOT be accepted as valid, or a dashboard panel referencing it would
        silently query a metric name that will never exist in production
        Prometheus.
        """
        mapping = check_dashboard_metrics.expected_prometheus_names(
            {"review.pipeline.step.duration_ms"}
        )

        assert mapping == {
            "review_pipeline_step_duration_ms_milliseconds": "review.pipeline.step.duration_ms",
        }
        assert "review_pipeline_step_duration_ms" not in mapping

    def test_redundant_total_token_yields_only_the_collapsed_form(self) -> None:
        """Case 1 from this module's own docstring — the other real
        normalization discovered by tests/e2e/test_observability.py: a
        counter name containing the word ``total`` outside its own
        mandatory trailing ``_total`` suffix is actually exported ONLY with
        that redundant occurrence dropped — the naive, un-collapsed form is
        never exported and must NOT be accepted as valid.
        """
        mapping = check_dashboard_metrics.expected_prometheus_names(
            {"review.prefilter.total_files_total"}
        )

        assert mapping == {
            "review_prefilter_files_total": "review.prefilter.total_files_total",
        }
        assert "review_prefilter_total_files_total" not in mapping

    def test_name_with_no_special_case_yields_only_the_naive_form(self) -> None:
        mapping = check_dashboard_metrics.expected_prometheus_names(
            {"review.context_builder.item_count"}
        )

        assert mapping == {"review_context_builder_item_count": "review.context_builder.item_count"}


class TestDashboardMetricTokens:
    def test_extracts_review_namespaced_tokens_from_nested_expr_fields(
        self, tmp_path: Path
    ) -> None:
        dashboard_path = tmp_path / "review.json"
        _write_dashboard(
            dashboard_path,
            [
                "sum(rate(review_queue_enqueued_total[5m]))",
                "histogram_quantile(0.95, "
                "sum(rate(review_pipeline_step_duration_ms_bucket[5m])) by (le))",
            ],
        )

        tokens = check_dashboard_metrics.dashboard_metric_tokens(dashboard_path)

        assert tokens == {
            "review_queue_enqueued_total",
            "review_pipeline_step_duration_ms_bucket",
        }

    def test_ignores_non_review_namespaced_promql_functions(self, tmp_path: Path) -> None:
        dashboard_path = tmp_path / "review.json"
        _write_dashboard(
            dashboard_path, ["sum(rate(review_queue_enqueued_total[5m])) by (job_type)"]
        )

        tokens = check_dashboard_metrics.dashboard_metric_tokens(dashboard_path)

        assert tokens == {"review_queue_enqueued_total"}


class TestFindDrift:
    def test_no_drift_when_every_token_matches(self) -> None:
        valid = {"review_queue_enqueued_total": "review.queue.enqueued_total"}

        offenders = check_dashboard_metrics.find_drift({"review_queue_enqueued_total"}, valid)

        assert offenders == []

    def test_histogram_suffix_is_stripped_before_matching(self) -> None:
        valid = {"review_pipeline_step_duration_ms": "review.pipeline.step.duration_ms"}

        offenders = check_dashboard_metrics.find_drift(
            {
                "review_pipeline_step_duration_ms_bucket",
                "review_pipeline_step_duration_ms_sum",
                "review_pipeline_step_duration_ms_count",
            },
            valid,
        )

        assert offenders == []

    def test_unregistered_token_is_reported_as_drift(self) -> None:
        valid = {"review_queue_enqueued_total": "review.queue.enqueued_total"}

        offenders = check_dashboard_metrics.find_drift({"review_queue_renamed_total"}, valid)

        assert offenders == ["review_queue_renamed_total"]

    def test_unregistered_token_with_histogram_like_suffix_still_reported(self) -> None:
        # "review_totally_made_up_metric_count" ends in "_count" but there is
        # no registered "review.totally.made_up.metric" — stripping the
        # suffix must not accidentally forgive a genuinely unregistered name.
        valid = {"review_queue_enqueued_total": "review.queue.enqueued_total"}

        offenders = check_dashboard_metrics.find_drift(
            {"review_totally_made_up_metric_count"}, valid
        )

        assert offenders == ["review_totally_made_up_metric_count"]


class TestCheckEndToEnd:
    def test_passing_case_raises_nothing(self, tmp_path: Path) -> None:
        _write_source_tree(
            tmp_path,
            ['meter.create_counter("review.queue.enqueued_total", description="x")'],
        )
        dashboard_path = tmp_path / "dashboards" / "review.json"
        _write_dashboard(dashboard_path, ["sum(rate(review_queue_enqueued_total[5m]))"])

        check_dashboard_metrics.check(
            dashboard_path=dashboard_path, repo_root=tmp_path, search_roots=("review",)
        )

    def test_drift_detected_case_raises_drift_error_naming_the_offender(
        self, tmp_path: Path
    ) -> None:
        _write_source_tree(
            tmp_path,
            ['meter.create_counter("review.queue.enqueued_total", description="x")'],
        )
        dashboard_path = tmp_path / "dashboards" / "review.json"
        _write_dashboard(
            dashboard_path,
            [
                "sum(rate(review_queue_enqueued_total[5m]))",
                "sum(rate(review_queue_renamed_total[5m]))",
            ],
        )

        with pytest.raises(check_dashboard_metrics.DriftError) as excinfo:
            check_dashboard_metrics.check(
                dashboard_path=dashboard_path, repo_root=tmp_path, search_roots=("review",)
            )

        assert excinfo.value.offenders == ["review_queue_renamed_total"]


class TestMainCLI:
    """Exercises `main()`'s argv/exit-code/stderr contract. `main()`
    deliberately exposes only `--dashboard` as CLI surface (matching its
    real CI usage) and resolves `repo_root`/`search_roots` from this
    module's own `REPO_ROOT`/`SEARCH_ROOTS` globals via `check()`'s
    call-time lookup (see that function's docstring) — so these tests
    monkeypatch those module attributes rather than passing them as argv.
    """

    def test_main_returns_zero_and_prints_ok_on_passing_dashboard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_source_tree(
            tmp_path,
            ['meter.create_counter("review.queue.enqueued_total", description="x")'],
        )
        dashboard_path = tmp_path / "dashboards" / "review.json"
        _write_dashboard(dashboard_path, ["sum(rate(review_queue_enqueued_total[5m]))"])
        monkeypatch.setattr(check_dashboard_metrics, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check_dashboard_metrics, "SEARCH_ROOTS", ("review",))

        exit_code = check_dashboard_metrics.main(["--dashboard", str(dashboard_path)])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "OK" in captured.out

    def test_main_returns_nonzero_and_prints_offenders_on_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_source_tree(
            tmp_path,
            ['meter.create_counter("review.queue.enqueued_total", description="x")'],
        )
        dashboard_path = tmp_path / "dashboards" / "review.json"
        _write_dashboard(dashboard_path, ["sum(rate(review_totally_unregistered_total[5m]))"])
        monkeypatch.setattr(check_dashboard_metrics, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check_dashboard_metrics, "SEARCH_ROOTS", ("review",))

        exit_code = check_dashboard_metrics.main(["--dashboard", str(dashboard_path)])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "review_totally_unregistered_total" in captured.err

    @pytest.mark.parametrize("name", ["../outside.json", "dashboard.txt"])
    def test_main_rejects_dashboard_path_outside_repo_or_not_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(check_dashboard_metrics, "REPO_ROOT", repo)

        with pytest.raises(SystemExit) as excinfo:
            check_dashboard_metrics.main(["--dashboard", str(repo / name)])

        assert excinfo.value.code == 2


class TestShippedDashboardCoversProductKnowledge:
    """
    US-PK14: the dashboard's product-knowledge row uses real instruments.

    It references only instruments the code registers (the existing guard, run
    against the real dashboard and the real source tree).
    """

    def test_real_dashboard_references_the_product_knowledge_instruments_and_has_no_drift(
        self,
    ) -> None:
        dashboard = check_dashboard_metrics.DEFAULT_DASHBOARD_PATH
        tokens = check_dashboard_metrics.dashboard_metric_tokens(dashboard)

        assert "review_product_knowledge_call_total" in tokens
        assert "review_product_knowledge_call_duration_ms_milliseconds_bucket" in tokens
        check_dashboard_metrics.check()  # raises DriftError on any drift

    def test_job_outcome_panel_splits_by_outcome_and_excludes_ok_and_not_configured(self) -> None:
        dashboard = json.loads(check_dashboard_metrics.DEFAULT_DASHBOARD_PATH.read_text())
        exprs = [
            target["expr"]
            for panel in dashboard["panels"]
            for target in panel.get("targets", [])
            if "review_product_knowledge_call_total" in target.get("expr", "")
            and 'operation="job"' in target["expr"]
        ]

        assert len(exprs) == 1
        assert "by (outcome)" in exprs[0]
        # not_configured is the steady state for most repos; plotting it
        # would bury the actionable config-class outcomes.
        assert 'outcome!~"ok|not_configured"' in exprs[0]
