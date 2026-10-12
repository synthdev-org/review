"""Unit test for US-F1's import-boundary requirement: no concrete step
module in ``review/`` imports another step module directly — only the
shared ``review.step.Step`` Protocol and the pydantic models passed
between them.

Per docs/adr/0005-pure-no-io-logic-unit-tested.md: a static AST check with
no real I/O of its own — Unit tier, regardless of which package it lives
in.

``review/`` has no concrete step modules yet in this phase (``review/step.py``,
``review/pipeline.py``, and ``review/config.py`` are the framework itself,
not steps — Phase 4 adds the first real one, ``review/prefilter.py``). The
scanning check below still runs against ``review/``'s actual contents today
(vacuously true over an empty set of step modules), establishing the
mechanism ahead of the first concrete step rather than waiting to add it
once there's something to check — and a second test proves the scanning
logic itself would actually catch a real violation, so the first test's
current vacuous pass isn't mistaken for the check being untested.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REVIEW_PACKAGE_DIR = Path(__file__).parent

# Modules that are the framework itself, not a concrete pipeline step —
# excluded from the "step module" scan below. Test files (`test_*.py`) are
# excluded separately.
#
# `context_symbols`/`context_bundle` (added by
# docs/plans/plan-deliver-context-builder.md's Phase 5 section) are Units
# A/D of the single `context_builder` macro step whose own `Step`
# implementation (`ContextBuilderStep`) lives in `review/context_builder.py`
# — internal helper modules of *one* macro step, split into separate files
# per this codebase's one-module-per-concern convention (Assumption 9), not
# sibling macro-step modules US-F1 exists to keep decoupled from each
# other. `context_builder.py` itself is deliberately *not* added here: it
# is the real macro-step module (it defines `ContextBuilderStep`), so it
# still needs scanning like `prefilter`/`static_analysis`/`llm_review`/
# `output` do.
#
# `citation` (added by that same plan's Phase 6 section) is, likewise, an
# internal helper module of the `output` macro step (`review.output`'s own
# `FindingsOutputStep` calls `render_citation` directly) — it defines no
# `Step` implementation of its own, so it's excluded from the scan the same
# way `context_symbols`/`context_bundle` are, not a sibling macro-step
# module `output.py`'s own import of it would otherwise (falsely) trip.
#
# `diff_prompt` (added by
# docs/plans/plan-deliver-verification-pipeline.md's Phase 1 section,
# Assumption 2) holds `render_diff`/`truncate_diff_text`, shared by every
# step module that builds an LLM prompt from a job's bare diff
# (`intent_summary`, `review_generation`, `verification`) — the same
# "internal helper module, no `Step` implementation of its own" shape as
# `context_symbols`/`context_bundle`/`citation` above, excluded here for
# the identical reason.
#
# `llm_response` holds `parse_llm_json_envelope`, shared by every step
# module that parses a batched LLM completion response against a fixed
# JSON envelope schema (`review_generation`, `verification`) — the same
# "internal helper module, no `Step` implementation of its own" shape as
# `diff_prompt` immediately above, excluded here for the identical reason.
#
# `_telemetry` holds `suppress_telemetry_errors`, shared by every step
# module that records a US1-era metric/span ("record telemetry, never let
# a failure to do so affect the real step outcome") — the same "internal
# helper module, no `Step` implementation of its own" shape as
# `diff_prompt`/`llm_response` above, excluded here for the identical
# reason. Deliberately private/underscore-prefixed and defined inside
# `review/` itself (never imported from `adapter/telemetry`) since domain
# packages never import adapter code (docs/rules/architecture.md).
#
# `llm_telemetry` holds `record_llm_token_usage` and the shared
# `review.llm.tokens_total` counter (US3), used by every step module that
# makes its own LLM completion call (`review_generation`, `verification`)
# — the same "internal helper module, no `Step` implementation of its own"
# shape as `diff_prompt`/`llm_response`/`_telemetry` above, excluded here
# for the identical reason.
_FRAMEWORK_MODULES = frozenset(
    {
        "__init__",
        "step",
        "pipeline",
        "config",
        "context_symbols",
        "context_bundle",
        "citation",
        # Pure product-knowledge wire models/logic (plan-deliver-product-
        # knowledge.md): a shared helper `context_builder` (provider branch)
        # and `context_bundle`/`citation` import, no `Step` of its own.
        "product_knowledge",
        "diff_prompt",
        "llm_response",
        "llm_telemetry",
        "_telemetry",
    }
)


def _step_module_names() -> frozenset[str]:
    """Every top-level module in ``review/`` that is a concrete pipeline
    step implementation, rather than the framework or a test file."""
    return frozenset(
        path.stem
        for path in _REVIEW_PACKAGE_DIR.glob("*.py")
        if path.stem not in _FRAMEWORK_MODULES and not path.stem.startswith("test_")
    )


def _imported_review_submodules(source: str) -> set[str]:
    """Every ``review.<x>`` submodule name a module's own source imports,
    however it spells the import: ``import review.x``,
    ``from review import x``, or a same-package relative
    ``from . import x`` / ``from .x import y``.
    """
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("review."):
                    imported.add(alias.name.split(".")[1])
        elif isinstance(node, ast.ImportFrom):
            if node.module == "review" or (node.module is None and node.level == 1):
                imported.update(alias.name for alias in node.names)
            elif node.module and node.module.startswith("review."):
                imported.add(node.module.split(".")[1])
            elif node.level == 1 and node.module:
                imported.add(node.module.split(".")[0])
    return imported


def test_no_step_module_imports_another_step_module_directly() -> None:
    step_modules = _step_module_names()
    for name in step_modules:
        source = (_REVIEW_PACKAGE_DIR / f"{name}.py").read_text()
        imported = _imported_review_submodules(source) & step_modules
        imported.discard(name)
        assert not imported, (
            f"review/{name}.py imports sibling step module(s) {sorted(imported)!r} "
            "directly; per US-F1, a step module may only depend on review.step's Step "
            "Protocol and the pydantic models passed between steps"
        )


def test_boundary_check_would_detect_a_real_violation() -> None:
    """Proves the scanning mechanism above would actually catch a
    violation, rather than the first test's pass being explained entirely
    by ``review/``'s current empty set of concrete step modules. Simulates
    two hypothetical step modules, ``prefilter`` and ``static_analysis``,
    with ``static_analysis`` importing ``prefilter`` directly — exactly the
    US-F1 violation the real scan exists to catch."""
    step_modules = frozenset({"prefilter", "static_analysis"})
    violating_source = "from review import prefilter\n"

    imported = _imported_review_submodules(violating_source) & step_modules
    imported.discard("static_analysis")

    assert imported == {"prefilter"}


def test_importing_the_framework_itself_is_not_a_violation() -> None:
    """A step module importing ``review.step`` (the shared Protocol) is
    exactly what US-F1 permits — the scan must not flag it, since it isn't
    a sibling *step* module."""
    step_modules = frozenset({"prefilter", "static_analysis"})
    source = "from review.step import Step, StepContext\n"

    imported = _imported_review_submodules(source) & step_modules

    assert imported == set()
