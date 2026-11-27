"""Unit tests for review.diff_links (pure, no I/O)."""

from __future__ import annotations

from review.diff_batching import CALL_WEIGHT, IMPORT_WEIGHT
from review.diff_links import build_file_links, module_aliases


def test_module_aliases() -> None:
    assert module_aliases("a/b/repo.ts") == {"repo"}
    assert module_aliases("a/b/index.ts") == {"index", "b"}
    assert module_aliases("pkg/store/db.go") == {"db", "store"}
    assert module_aliases("a/__init__.py") == {"__init__", "a"}


def test_typescript_import_and_call_links() -> None:
    contents = {
        "src/service.ts": (
            b"import { persist } from './repo';\nexport function go() { persist(1); }\n"
        ),
        "src/repo.ts": b"export function persist(x: number) { return x; }\n",
    }
    result = build_file_links(contents)
    assert result.unparsed == ()
    # import of `persist` (module-level) plus the call inside `go`.
    assert result.links == {"src/service.ts": {"src/repo.ts": IMPORT_WEIGHT + CALL_WEIGHT}}


def test_php_use_links_to_defining_class() -> None:
    contents = {
        "a/Controller.php": b"<?php\nuse App\\Saver;\nclass Controller {}\n",
        "a/Saver.php": b"<?php\nclass Saver { function store() {} }\n",
    }
    assert build_file_links(contents).links == {"a/Controller.php": {"a/Saver.php": IMPORT_WEIGHT}}


def test_go_call_links_to_defining_file() -> None:
    contents = {
        "app/handler.go": b"package app\nfunc Handle2() { Persist() }\n",
        "app/store.go": b"package app\nfunc Persist() {}\n",
    }
    assert build_file_links(contents).links == {"app/handler.go": {"app/store.go": CALL_WEIGHT}}


def test_generic_short_and_ambiguous_names_are_ignored() -> None:
    contents = {
        "a.ts": b"export function get() {}\nexport function ab() {}\nexport function dup() {}\n",
        "b.ts": b"export function dup() {}\n",
        "c.ts": b"export function dup() {}\n",
        "d.ts": b"export function dup() {}\n",
        "user.ts": b"function f() { get(); ab(); dup(); }\n",
    }
    assert build_file_links(contents).links == {}


def test_unsupported_and_invalid_files_are_unparsed_not_fatal() -> None:
    contents = {
        "README.md": b"# hi",
        "bad.ts": b"\xff\xfe\x00bad",
        "ok.ts": b"export function fine() {}\n",
    }
    result = build_file_links(contents)
    assert set(result.unparsed) == {"README.md", "bad.ts"}
    assert result.links == {}


def test_self_references_do_not_link() -> None:
    contents = {"x.ts": b"function helper() {}\nfunction go() { helper(); }\n"}
    assert build_file_links(contents).links == {}


def test_python_import_links_to_module() -> None:
    contents = {
        "app/service.py": b"from app.repo import persist\n\n\ndef go():\n    persist(1)\n",
        "app/repo.py": b"def persist(x):\n    return x\n",
    }
    result = build_file_links(contents)
    assert result.unparsed == ()
    assert result.links["app/service.py"]["app/repo.py"] >= IMPORT_WEIGHT


def test_module_level_call_weighs_as_a_call_not_an_import() -> None:
    contents = {
        "a.py": b"compute_total()\n",
        "b.py": b"def compute_total(): pass\n",
        "c.py": b"def g():\n    compute_total()\n",
    }
    links = build_file_links(contents).links
    assert links == {"a.py": {"b.py": CALL_WEIGHT}, "c.py": {"b.py": CALL_WEIGHT}}
