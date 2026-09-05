"""Tests for indexing.prefilter — US9, per the plan's Phase 3 section (Unit
C) and its own Acceptance criterion. Unit tier, pure logic, no I/O.
"""

from __future__ import annotations

from indexing.prefilter import filter_indexable_paths, is_indexable_path

# ---------------------------------------------------------------------------
# US9's own Given/When/Then: go.sum, a node_modules/ path, and a .py file
# never reach the chunker; in-scope extensions do.
# ---------------------------------------------------------------------------


def test_us9_given_when_then() -> None:
    paths = (
        "go.sum",
        "frontend/node_modules/react/index.js",
        "scripts/build.py",
        "src/app.ts",
    )

    surviving = filter_indexable_paths(paths)

    assert surviving == ("src/app.ts",)


def test_in_scope_extensions_all_survive() -> None:
    paths = ("src/app.ts", "src/App.tsx", "src/Widget.php", "cmd/main.go")

    surviving = filter_indexable_paths(paths)

    assert surviving == paths


# ---------------------------------------------------------------------------
# Extension filter — in scope vs. out of scope
# ---------------------------------------------------------------------------


def test_unsupported_extension_is_excluded() -> None:
    assert is_indexable_path("README.md") is False
    assert is_indexable_path("scripts/build.py") is False
    assert is_indexable_path("Makefile") is False


def test_each_in_scope_extension_is_included() -> None:
    assert is_indexable_path("src/app.ts") is True
    assert is_indexable_path("src/App.tsx") is True
    assert is_indexable_path("src/Widget.php") is True
    assert is_indexable_path("cmd/main.go") is True


# ---------------------------------------------------------------------------
# Ignore-pattern categories — each as its own case, mirroring
# review/test_prefilter_step.py's own per-category coverage.
# ---------------------------------------------------------------------------


def test_lockfile_is_excluded() -> None:
    assert is_indexable_path("go.sum") is False
    assert is_indexable_path("nested/dir/go.sum") is False


def test_vendored_path_is_excluded() -> None:
    assert is_indexable_path("vendor/some_lib/main.go") is False
    assert is_indexable_path("frontend/node_modules/react/index.ts") is False


def test_generated_file_is_excluded() -> None:
    assert is_indexable_path("api/thing.pb.go") is False


def test_binary_extension_is_excluded() -> None:
    # Binary-extension globs don't overlap this phase's in-scope
    # extensions in practice, but the check still runs first regardless.
    assert is_indexable_path("assets/logo.png") is False


def test_order_is_preserved() -> None:
    paths = ("cmd/main.go", "go.sum", "src/app.ts", "scripts/build.py", "src/Widget.php")

    surviving = filter_indexable_paths(paths)

    assert surviving == ("cmd/main.go", "src/app.ts", "src/Widget.php")
