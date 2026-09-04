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
_FRAMEWORK_MODULES = frozenset({"__init__", "step", "pipeline", "config"})


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
