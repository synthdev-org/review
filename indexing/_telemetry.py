"""indexing._telemetry — a private, indexing-package-local helper for the
"record a metric/span, never let a failure to do so affect the real
pipeline outcome" shape indexing/pipeline.py's own instrumentation needs.

No infra imports beyond the OTel API itself, per docs/rules/architecture.md's
domain-package rule — the OTel API import is explicitly permitted in domain
packages by docs/adr/0009-otel-api-imported-directly-in-domain-packages.md.
Deliberately scoped to this one bounded context rather than a cross-context
``adapter/telemetry`` import: domain packages never import adapter code.
Mirrors ``review/_telemetry.py``'s own copy of this same shape, for the
same reason — see that module's docstring for the full rationale.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager

from opentelemetry import trace


def start_span(tracer: trace.Tracer, name: str) -> AbstractContextManager[trace.Span]:
    """``tracer.start_as_current_span(name, ...)`` with a fixed
    ``record_exception=False``/``set_status_on_exception=False`` pair — every
    span this package creates disables both for the same Privacy/Compliance
    reason (a caught exception's ``str()`` must never become span data), so
    one helper keeps that pair from drifting between call sites. The caller
    supplies its own module-level ``tracer`` so span provenance still
    reflects the calling module, not this shim. Mirrors
    ``review._telemetry.start_span`` — see that module's docstring for the
    full rationale.
    """
    return tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False)


@contextmanager
def suppress_telemetry_errors(
    logger: logging.Logger, message: str, **extra: object
) -> Iterator[None]:
    """Runs the wrapped block; any exception it raises is caught and logged
    as a warning (``exc_info=True``) rather than propagated, so a
    telemetry-recording failure can never be mistaken for the real
    operation it's reporting on having failed.

    This deliberately deviates from ``architecture.md``'s "never swallow
    or log-and-continue silently" rule — see
    ``docs/adr/0010-telemetry-recording-failures-log-and-continue.md`` for
    the formal reasoning and exact scope of the exemption.

    Args:
        logger: the caller's own module logger.
        message: the warning message to log on failure.
        **extra: passed through verbatim as the log record's ``extra`` dict.
    """
    try:
        yield
    except Exception:
        logger.warning(message, extra=extra, exc_info=True)
