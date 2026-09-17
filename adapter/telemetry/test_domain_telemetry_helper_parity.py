"""Parity guard for telemetry helper functions this repo deliberately
duplicates per bounded-context domain package.

``review._telemetry.suppress_telemetry_errors``,
``indexing._telemetry.suppress_telemetry_errors``, and
``adapter.telemetry.sdk.suppress_telemetry_errors`` are three independent,
byte-for-byte-identical implementations of the same "log and continue"
context manager — required by docs/rules/architecture.md's "the domain
never imports entrypoint or adapter code" rule (neither ``review`` nor
``indexing`` may import the adapter's copy) plus DDD's bounded-context
separation (neither domain package may import the other's private helper
either, and no shared ``common``/``utils`` package is allowed to straddle
both) — see each domain module's own docstring for the full reasoning.
``review.pipeline._start_span``/``indexing.pipeline._start_span`` are
duplicated for the identical reason, one layer up (each closes over its
own module-level ``tracer`` handle per
docs/adr/0009-otel-api-imported-directly-in-domain-packages.md).

Physical duplication is the architecturally correct shape here, not an
oversight — but it does mean a future change to either helper's contract
has to land in every copy by hand, with nothing at import time to catch a
drift. These tests are that catch: each fails loudly the moment any one
copy's *behavior* (not docstring wording) diverges from its siblings,
rather than letting a silent drift ship.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable

import adapter.telemetry.sdk as adapter_telemetry_sdk
import indexing._telemetry as indexing_telemetry
import indexing.pipeline as indexing_pipeline
import review._telemetry as review_telemetry
import review.pipeline as review_pipeline


def _behavioral_body(fn: Callable[..., object]) -> str:
    """The function's body, docstring stripped, as a canonical ``ast.dump``
    string — isolates actual behavior from per-module docstring wording,
    which is expected (and fine) to differ between copies.
    """
    source = textwrap.dedent(inspect.getsource(fn))
    func_def = ast.parse(source).body[0]
    assert isinstance(func_def, ast.FunctionDef)
    body = func_def.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.dump(stmt) for stmt in body)


def _assert_bodies_match(name: str, bodies: dict[str, str]) -> None:
    distinct = set(bodies.values())
    assert len(distinct) == 1, (
        f"{name}'s intentionally-duplicated copies (architecturally "
        f"required per docs/rules/architecture.md's domain-package "
        f"boundary) have drifted apart: {bodies}"
    )


def test_suppress_telemetry_errors_bodies_stay_in_sync() -> None:
    _assert_bodies_match(
        "suppress_telemetry_errors",
        {
            "review._telemetry": _behavioral_body(review_telemetry.suppress_telemetry_errors),
            "indexing._telemetry": _behavioral_body(indexing_telemetry.suppress_telemetry_errors),
            "adapter.telemetry.sdk": _behavioral_body(
                adapter_telemetry_sdk.suppress_telemetry_errors
            ),
        },
    )


def test_start_span_bodies_stay_in_sync() -> None:
    _assert_bodies_match(
        "_start_span",
        {
            "review.pipeline": _behavioral_body(review_pipeline._start_span),
            "indexing.pipeline": _behavioral_body(indexing_pipeline._start_span),
        },
    )
