"""Dashboard/metric drift guard (US13, docs/specs/spec-observability.md).

Parses every metric name referenced in the shipped Grafana dashboard's
panel PromQL queries (``charts/review/dashboards/review.json``), and
cross-checks each one against the full set of OTel metric names actually
registered in the instrumented source tree (``review/``, ``adapter/``,
``apps/``, ``indexing/``, ``findings/`` — every ``.create_counter(...)``,
``.create_histogram(...)``, ``.create_up_down_counter(...)``,
``.create_gauge(...)``, and ``.create_observable_*(...)`` call). Fails
loudly (non-zero exit, naming every offending metric) if the dashboard
references a name the code doesn't register, so a future rename in either
place can't silently break the other.

Pure Python, no network/DB I/O — only reading the dashboard JSON and
grepping the source tree, matching this repo's `scripts/` directory
convention (shell-script-heavy, Python tool acceptable per
docs/specs/spec-observability.md's Unit E deliverables).

The dashboard's PromQL queries use Prometheus's underscore form of each
metric name (e.g. ``review_pipeline_step_duration_ms``); the OTel
Collector's Prometheus exporter produces that form from the original
dotted OTel name (e.g. ``review.pipeline.step.duration_ms``) by replacing
every ``.`` with ``_`` (existing underscores in the original name are left
alone — e.g. ``review.queue.enqueued_total`` becomes
``review_queue_enqueued_total``, not something reversible by a blind
``_`` -> ``.`` substitution, since the original name already contains
underscores of its own). Rather than trying to invert that mapping, this
script computes the *forward* mapping from every registered dotted name
and checks dashboard-referenced tokens against that computed set.

Two further real (not assumed) normalizations the Collector's Prometheus
exporter applies, both discovered by Unit F's own E2E run against a real
Collector/Prometheus/Grafana stack (docs/specs/spec-observability.md's
``tests/e2e/test_observability.py``) — this script models each narrowly,
as an *additional* candidate Prometheus name per registered metric, never
replacing the naive one, so neither can widen what the naive rule alone
would accept beyond the specific case each documents:

1. For a monotonic counter, the translated name already ends in
   ``_total`` (the counter-type suffix); if the literal word ``total``
   also appears earlier in that same underscore-split name — e.g.
   ``review.prefilter.total_files_total`` -> naive
   ``review_prefilter_total_files_total`` — the exporter drops that
   earlier, now-redundant occurrence, producing
   ``review_prefilter_files_total`` instead
   (``_collapse_redundant_total_token``).
2. For a histogram declared with ``unit="ms"``, the exporter appends the
   unit's full Prometheus-convention word (``milliseconds``) as a name
   suffix — and, unlike the ``By``/bytes and ``s``/seconds units this
   codebase also uses (both already end their own metric names with that
   same full word, so the exporter's own de-duplication skips
   re-appending it), a name ending in the short code ``_ms`` doesn't
   textually match the full word ``milliseconds``, so no de-duplication
   fires: ``review.pipeline.step.duration_ms`` -> naive
   ``review_pipeline_step_duration_ms`` actually exports as
   ``review_pipeline_step_duration_ms_milliseconds``
   (``_append_milliseconds_suffix_for_ms_histograms``).

Prometheus's histogram/summary convention also appends ``_bucket``,
``_sum``, or ``_count`` to a histogram metric's base name for its
constituent series (e.g. ``review_pipeline_step_duration_ms_bucket``) —
this script strips a known suffix before checking membership, but only
when the un-suffixed form is itself a valid registered name, so it can
never accidentally forgive a genuinely unregistered metric that happens to
end in one of those words.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DASHBOARD_PATH = REPO_ROOT / "charts" / "review" / "dashboards" / "review.json"

# The instrumented-code package roots the METRIC & SPAN CATALOG's own
# "grep for ... across review/, adapter/, apps/, indexing/, findings/"
# instruction names, verbatim.
SEARCH_ROOTS: tuple[str, ...] = ("review", "adapter", "apps", "indexing", "findings")

# Matches `<meter>.create_counter("name", ...)`, `.create_histogram(...)`,
# `.create_up_down_counter(...)`, `.create_gauge(...)`, and any
# `.create_observable_*(...)` call, capturing the first string-literal
# argument (single or double quoted) regardless of how far it is from the
# opening paren (this codebase sometimes puts the name on its own line).
INSTRUMENT_CALL_RE = re.compile(
    r"\.create_(?:counter|histogram|up_down_counter|gauge|observable_\w+)\(\s*"
    r"[\"']([^\"']+)[\"']"
)

# Candidate PromQL metric-name tokens: this project's own metric namespace
# always starts with "review_" (every catalog entry starts with
# "review." before dot-to-underscore translation), so scoping the token
# regex to that prefix avoids false positives on PromQL functions/keywords
# (sum, rate, histogram_quantile, by, le, ...) without needing a full PromQL
# parser.
PROM_METRIC_TOKEN_RE = re.compile(r"\breview_[a-zA-Z0-9_]*\b")

# Prometheus's histogram/summary convention: one logical metric name
# produces multiple concrete series with these suffixes appended.
HISTOGRAM_SUFFIXES: tuple[str, ...] = ("_bucket", "_sum", "_count")


class DriftError(Exception):
    """Raised when the dashboard references a metric name absent from the
    instrumented code's registered metric set."""

    def __init__(self, offenders: list[str], registered: set[str]) -> None:
        self.offenders = offenders
        self.registered = registered
        super().__init__(
            "Dashboard references metric name(s) with no matching registered "
            f"metric: {', '.join(offenders)}"
        )


def registered_metric_names(
    repo_root: Path, search_roots: tuple[str, ...] = SEARCH_ROOTS
) -> set[str]:
    """Every OTel metric name string literal registered anywhere under
    ``search_roots``, in its original dotted OTel form exactly as passed to
    ``create_counter``/``create_histogram``/``create_up_down_counter``/
    ``create_gauge``/``create_observable_*``. Test files (``test_*.py``)
    are excluded — a name that only appears in a test's own fake/mock
    instrument doesn't count as "registered in production code".
    """
    names: set[str] = set()
    for root_name in search_roots:
        root = repo_root / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if path.name.startswith("test_") or path.name.endswith("_test.py"):
                continue
            text = path.read_text(encoding="utf-8")
            names.update(INSTRUMENT_CALL_RE.findall(text))
    return names


def _collapse_redundant_total_token(underscored_name: str) -> str:
    """Models the one real Collector-Prometheus-exporter normalization
    beyond a naive ``.`` -> ``_`` mapping this script accounts for — see
    this module's own docstring for how it was discovered and why it's
    safe to apply unconditionally: a no-op unless ``underscored_name``
    both ends in ``_total`` *and* contains another standalone ``total``
    token earlier in the name, in which case only the trailing occurrence
    survives.
    """
    tokens = underscored_name.split("_")
    if len(tokens) < 2 or tokens[-1] != "total":
        return underscored_name
    head, tail = tokens[:-1], tokens[-1]
    if "total" not in head:
        return underscored_name
    # Collapse only the single redundant occurrence this normalization
    # models (see docstring) — removing every "total" token in the head
    # would over-collapse a hypothetical name with more than one
    # legitimate "total" segment.
    collapsed_head = list(head)
    collapsed_head.remove("total")
    return "_".join([*collapsed_head, tail])


def _append_milliseconds_suffix_for_ms_histograms(underscored_name: str) -> str:
    """Models this script's own second documented real normalization — see
    the module docstring's case 2. A no-op unless ``underscored_name``
    ends in the short unit code ``_ms``, in which case the exporter's own
    full-word unit suffix (``milliseconds``) is appended, since a
    trailing ``_ms`` doesn't textually match ``milliseconds`` for the
    exporter's own de-duplication to skip it.

    Narrowly scoped to the one unit code (``ms``) this codebase's own
    catalog actually names a histogram after (``_ms``) — not a general
    OTel-unit-to-Prometheus-suffix table, since the other units this
    codebase uses (``s``/seconds, ``By``/bytes) already name their own
    metrics with the matching full word and hit the exporter's real
    de-duplication rather than this script's.
    """
    if not underscored_name.endswith("_ms"):
        return underscored_name
    return f"{underscored_name}_milliseconds"


def expected_prometheus_names(dotted_names: set[str]) -> dict[str, str]:
    """Forward-map every registered dotted OTel name to the *single*
    Prometheus metric family name the OTel Collector's Prometheus exporter
    is actually expected to produce for it, keyed by that Prometheus name
    for O(1) lookup, valued by the original dotted name (useful for error
    messages / round-tripping).

    Deliberately NOT the naive ``.`` -> ``_`` mapping plus every
    normalized form as additional, always-valid candidates: when a real
    normalization applies (``_collapse_redundant_total_token``,
    ``_append_milliseconds_suffix_for_ms_histograms``), the naive,
    un-normalized form is never actually exported by the Collector, so it
    must not be accepted as a valid dashboard reference — doing so would
    let a dashboard panel reference a metric name that will never exist in
    production Prometheus while this drift guard reports "OK". Each
    normalization function is a no-op unless the name hits its specific
    real case, so applying both in sequence and keeping only the final
    result is safe and still yields exactly one Prometheus name per
    registered metric.
    """
    mapping: dict[str, str] = {}
    for name in dotted_names:
        naive = name.replace(".", "_")
        normalized = _append_milliseconds_suffix_for_ms_histograms(
            _collapse_redundant_total_token(naive)
        )
        mapping[normalized] = name
    return mapping


def dashboard_metric_tokens(dashboard_path: Path) -> set[str]:
    """Every ``review_``-namespaced token referenced in any panel's PromQL
    ``expr`` field, anywhere in the dashboard JSON's panel tree (including
    nested/collapsed rows)."""
    data = json.loads(dashboard_path.read_text(encoding="utf-8"))
    tokens: set[str] = set()

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            expr = node.get("expr")
            if isinstance(expr, str):
                tokens.update(PROM_METRIC_TOKEN_RE.findall(expr))
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return tokens


def find_drift(dashboard_tokens: set[str], valid_prometheus_names: dict[str, str]) -> list[str]:
    """Every dashboard-referenced token that cannot be resolved to a
    registered metric name, either directly or by stripping one Prometheus
    histogram/summary suffix (``_bucket``/``_sum``/``_count``). Returned
    sorted, for deterministic error output."""
    offenders: list[str] = []
    for token in sorted(dashboard_tokens):
        if token in valid_prometheus_names:
            continue
        resolved = False
        for suffix in HISTOGRAM_SUFFIXES:
            if token.endswith(suffix) and token[: -len(suffix)] in valid_prometheus_names:
                resolved = True
                break
        if not resolved:
            offenders.append(token)
    return offenders


def check(
    dashboard_path: Path | None = None,
    repo_root: Path | None = None,
    search_roots: tuple[str, ...] | None = None,
) -> None:
    """Run the full drift check. Raises :class:`DriftError` on drift;
    returns ``None`` silently on success.

    ``None`` defaults are resolved against this module's own
    ``DEFAULT_DASHBOARD_PATH``/``REPO_ROOT``/``SEARCH_ROOTS`` globals *at
    call time* (a dynamic lookup, not a value bound once at import time) —
    deliberately, so a test can `monkeypatch.setattr` those module
    attributes and have it take effect on a subsequent unparameterized
    ``check()``/``main()`` call, the same way `main()`'s own CLI entrypoint
    relies on it.
    """
    if dashboard_path is None:
        dashboard_path = DEFAULT_DASHBOARD_PATH
    if repo_root is None:
        repo_root = REPO_ROOT
    if search_roots is None:
        search_roots = SEARCH_ROOTS
    dotted_names = registered_metric_names(repo_root, search_roots)
    valid_prometheus_names = expected_prometheus_names(dotted_names)
    dashboard_tokens = dashboard_metric_tokens(dashboard_path)
    offenders = find_drift(dashboard_tokens, valid_prometheus_names)
    if offenders:
        raise DriftError(offenders, dotted_names)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dashboard",
        type=Path,
        default=DEFAULT_DASHBOARD_PATH,
        help="Path to the dashboard JSON (default: charts/review/dashboards/review.json)",
    )
    args = parser.parse_args(argv)

    try:
        check(dashboard_path=args.dashboard)
    except DriftError as exc:
        print(
            "Dashboard/metric drift detected — the following metric name(s) "
            f"referenced in {args.dashboard} do not correspond to any metric "
            "registered in the instrumented source tree:",
            file=sys.stderr,
        )
        for name in exc.offenders:
            print(f"  - {name}", file=sys.stderr)
        print(
            f"\nRegistered metric names (dotted OTel form, {len(exc.registered)} total):",
            file=sys.stderr,
        )
        for name in sorted(exc.registered):
            print(f"  - {name}", file=sys.stderr)
        return 1

    print("OK: every metric name referenced in the dashboard resolves to a registered metric.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
