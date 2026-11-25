"""adapter.telemetry.prometheus_reader — ``PrometheusMetricsReader`` (Unit
G, US-A17), per ``docs/plans/plan-deliver-admin-gui.md``'s Phase 6 section
and Assumption 4.

Implements ``apps.admin_api.stats_service.MetricsQuerier`` (a narrow,
one-method Protocol defined at its consumer, ``apps/admin_api`` — see that
module's own docstring for why it lives there rather than at a domain
package) by calling Prometheus's HTTP API ``/api/v1/query`` endpoint
(instant query) and parsing its response into typed
``apps.admin_api.stats_service.PrometheusSample`` values, per
``docs/rules/architecture.md``'s "validate every boundary... with pydantic
models" rule.

**Reuses ``adapter/github/http.py``'s shared httpx retry/backoff +
redacted-logging wrapper (``request_with_retry``/``client_context``)
rather than hand-rolling a second one** — per Assumption 4, this repo's own
"one shared implementation, not N independent copies" precedent for
exactly the transient-failure shape (slow/unreachable endpoint, bounded
retry, no hang) this reader also needs to handle. Despite that module's
name, its retry/backoff policy and redacted logging are generic
httpx-call concerns, not GitHub-specific ones — nothing in it references a
GitHub-specific URL, header, or payload shape. Its own raised exceptions
(``GitHubPermanentAPIError``/``GitHubRetryExhaustedError``) are caught here
and translated into this module's own ``PrometheusUnreachableError``, so no
GitHub-named exception type ever escapes this adapter's own contract.

**A deliberate, narrowly-scoped exception to the usual adapter -> domain
import direction:** this module imports ``PrometheusSample`` from
``apps.admin_api.stats_service`` — an entrypoint module, not a domain
package — because Assumption 4 places ``MetricsQuerier`` at its one real
consumer (``apps/admin_api``) rather than inventing a domain package for
logic with "no reusable domain rules, single consumer" (that module's own
docstring). No circularity results: ``apps/admin_api/stats_service.py``
itself imports nothing from this module or from ``adapter`` at all: the
concrete ``PrometheusMetricsReader`` instance is wired to the
``MetricsQuerier``-typed parameter only at ``apps/admin_api/main.py``'s own
composition root, per ``docs/rules/architecture.md``'s constructor-injection
rule.

Never hangs: every call is made through an ``httpx.AsyncClient`` carrying
an explicit, finite timeout, and ``request_with_retry``'s own retry budget
(``max_attempts``) bounds the number of attempts — a persistently
unreachable Prometheus fails with ``PrometheusUnreachableError`` after a
bounded amount of wall-clock time, never an unbounded wait, per the
Reliability NFR ``apps/admin_api/stats_routes.py``'s own "clean 5xx, never
a hang" contract depends on.
"""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from adapter.github.exceptions import GitHubPermanentAPIError, GitHubRetryExhaustedError
from adapter.github.http import client_context, request_with_retry
from adapter.telemetry.exceptions import PrometheusUnreachableError
from apps.admin_api.stats_service import PrometheusSample

logger = logging.getLogger(__name__)

# Conservative, bounded defaults — this reader serves one read-only
# dashboard endpoint, not a latency-critical job-dispatch path, so these
# lean toward "fail within a few seconds" over "match GitHub's own 10s/3
# attempts" (adapter/github/diff.py's defaults) — a slower Prometheus still
# shouldn't make GET /api/v1/stats visibly hang for tens of seconds.
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_RETRY_MAX_ATTEMPTS = 2


class _PrometheusResultEntry(BaseModel):
    """One entry in Prometheus's own ``data.result`` array for an instant
    (``resultType: "vector"``) query — ``metric`` is the entry's label set
    (empty ``{}`` for a label-free aggregate expression, e.g. a bare
    ``sum(...)``), ``value`` is Prometheus's own ``[unix_timestamp,
    "string_value"]`` pair (the value is always transmitted as a JSON
    string, per Prometheus's own HTTP API documentation, hence ``str`` here
    rather than ``float``).
    """

    model_config = ConfigDict(extra="ignore")

    metric: dict[str, str] = {}
    value: tuple[float, str]


class _PrometheusQueryData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    resultType: str
    result: list[_PrometheusResultEntry] = []


class _PrometheusQueryResponse(BaseModel):
    """The full ``/api/v1/query`` response body — ``data`` is present only
    when ``status == "success"``; ``error``/``errorType`` only when
    ``status == "error"`` (Prometheus's own documented shape)."""

    model_config = ConfigDict(extra="ignore")

    status: str
    data: _PrometheusQueryData | None = None
    error: str | None = None
    errorType: str | None = None


class PrometheusMetricsReader:
    """``apps.admin_api.stats_service.MetricsQuerier`` implementation
    backed by a real Prometheus HTTP API endpoint. Constructed once at the
    composition root (``apps/admin_api/main.py``) — a single long-lived
    Admin API process serves many ``GET /api/v1/stats`` requests over its
    lifetime, so this instance carries no per-request state.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retry_max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._retry_max_attempts = retry_max_attempts
        # Optional shared, long-lived client (constructed once at the
        # composition root). Defaults to None, matching
        # adapter/github/diff.py's GitHubDiffFetcher's own convention — see
        # adapter/github/http.py's client_context for the "shared if given,
        # else a fresh throwaway one" behavior this relies on.
        self._client = client

    async def query(self, promql: str) -> tuple[PrometheusSample, ...]:
        """Runs one Prometheus instant query
        (``GET {base_url}/api/v1/query?query=<promql>``) and returns every
        label-set/value pair its ``data.result`` array carries — an empty
        tuple for a query whose result set is genuinely empty (e.g. a
        counter that has never been recorded), never an error: an operator
        opening the dashboard before the platform has processed any jobs
        should see zeros, not a failure.

        Raises ``PrometheusUnreachableError`` for every other failure mode
        (connection failure, retry-budget exhaustion, a non-2xx response, a
        response body that isn't valid JSON matching the expected shape, or
        a well-formed body whose own ``status`` field is ``"error"``) — one
        exception type, since ``apps/admin_api/stats_routes.py``'s own
        "clean 5xx, never a hang" contract treats all of these identically.
        """
        url = f"{self._base_url}/api/v1/query"
        try:
            async with client_context(self._client, timeout=self._timeout_seconds) as client:
                response = await request_with_retry(
                    client,
                    "GET",
                    url,
                    params={"query": promql},
                    max_attempts=self._retry_max_attempts,
                )
        except (GitHubPermanentAPIError, GitHubRetryExhaustedError) as exc:
            logger.error("prometheus query failed", extra={"promql": promql, "error": str(exc)})
            raise PrometheusUnreachableError(detail=str(exc)) from exc

        try:
            parsed = _PrometheusQueryResponse.model_validate(response.json())
        except ValidationError as exc:
            logger.error(
                "prometheus query returned a malformed response body",
                extra={"promql": promql, "error": str(exc)},
            )
            raise PrometheusUnreachableError(detail=f"malformed response body: {exc}") from exc

        if parsed.status != "success" or parsed.data is None:
            logger.error(
                "prometheus reported a query error",
                extra={
                    "promql": promql,
                    "status": parsed.status,
                    "error_type": parsed.errorType,
                    "error": parsed.error,
                },
            )
            raise PrometheusUnreachableError(
                detail=f"prometheus reported status={parsed.status!r} error={parsed.error!r}"
            )

        return tuple(
            PrometheusSample(labels=entry.metric, value=float(entry.value[1]))
            for entry in parsed.data.result
        )
