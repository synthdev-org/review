"""Unit tests for ``indexing.clone`` — the clone-detection fingerprint engine
(Phase 10, Unit A; plan ``docs/plans/plan-phase-10.md`` Phase 1).

Pure, no-I/O logic: Unit tier per ADR-0005. Fixtures live in
``indexing/testdata/clone/`` — per language an original, a renamed-identifier
copy, a comment/whitespace variant, a structure-changed non-copy and an
imports-only file, plus ``coverage.*`` files that exercise as much grammar
surface as possible for the tokenizer's completeness test.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import zlib
from itertools import pairwise
from pathlib import Path

import pytest

from indexing import clone
from indexing.chunk import ChunkLanguage, language_for_path, parse_file
from indexing.clone import (
    GRAM_TOKENS,
    GUARANTEE_TOKENS,
    WINDOW,
    CloneFileIndex,
    CloneIndexError,
    CloneRegion,
    Fingerprint,
    build_clone_index,
    fingerprints,
    is_excluded_path,
    is_test_path,
    match_regions,
    region_is_import_only,
    tokenize,
)

_TESTDATA = Path(__file__).parent / "testdata" / "clone"
_LANGUAGE_EXTENSIONS = ("ts", "tsx", "php", "go")


def _load(name: str) -> tuple[ChunkLanguage, str, bytes]:
    path = f"src/{name}"
    language = language_for_path(path)
    assert language is not None, name
    return language, path, (_TESTDATA / name).read_bytes()


def _index(name: str) -> tuple[CloneFileIndex, clone.NormalizedTokens]:
    language, path, content = _load(name)
    return build_clone_index(language, path, parse_file(language, path, content))


def _regions(query: str, stored: str) -> tuple[CloneRegion, ...]:
    return match_regions(_index(query)[0], _index(stored)[0])


# --- US-C7: Type-2 clones match; different structure does not -------------


@pytest.mark.parametrize("ext", _LANGUAGE_EXTENSIONS)
def test_renamed_identifiers_and_edited_literals_match_end_to_end(ext: str) -> None:
    query, _ = _index(f"renamed.{ext}")
    regions = _regions(f"renamed.{ext}", f"original.{ext}")
    assert len(regions) == 1
    assert regions[0].token_count == len(query.token_hashes)
    assert regions[0].query_start_line == 1


@pytest.mark.parametrize("ext", _LANGUAGE_EXTENSIONS)
def test_comments_and_whitespace_do_not_affect_the_match(ext: str) -> None:
    original, _ = _index(f"original.{ext}")
    commented, _ = _index(f"comments.{ext}")
    assert commented.token_hashes == original.token_hashes
    regions = _regions(f"comments.{ext}", f"original.{ext}")
    assert [region.token_count for region in regions] == [len(original.token_hashes)]


@pytest.mark.parametrize("ext", _LANGUAGE_EXTENSIONS)
def test_changed_operators_and_control_flow_do_not_match(ext: str) -> None:
    assert _regions(f"restructured.{ext}", f"original.{ext}") == ()


def test_a_literal_only_edit_still_matches() -> None:
    first = clone.tokenize(
        "typescript", "a.ts", parse_file("typescript", "a.ts", b"const x = 1;\n")
    )
    second = clone.tokenize(
        "typescript", "b.ts", parse_file("typescript", "b.ts", b'const x = "text";\n')
    )
    assert first.hashes == second.hashes


def test_a_renamed_identifier_still_matches_but_a_changed_keyword_does_not() -> None:
    def hashes(source: bytes) -> tuple[int, ...]:
        return tokenize("go", "x.go", parse_file("go", "x.go", source)).hashes

    base = hashes(b"package p\nfunc f() { return a + b }\n")
    assert hashes(b"package p\nfunc g() { return c + d }\n") == base
    assert hashes(b"package p\nfunc f() { return a - b }\n") != base


# --- US-C8: size, span, boilerplate, paths --------------------------------


def test_a_run_below_the_token_minimum_is_not_reported() -> None:
    query, _ = _index("renamed.ts")
    stored, _ = _index("original.ts")
    assert match_regions(query, stored, min_tokens=len(query.token_hashes) + 1) == ()


def test_a_run_below_the_line_minimum_is_not_reported() -> None:
    query, _ = _index("renamed.ts")
    stored, _ = _index("original.ts")
    assert match_regions(query, stored, min_lines=10_000) == ()


@pytest.mark.parametrize("ext", _LANGUAGE_EXTENSIONS)
def test_an_import_block_is_import_only_but_the_whole_file_need_not_be(ext: str) -> None:
    _, tokens = _index(f"imports.{ext}")
    start = tokens.in_import.index(True)
    end = start
    while end < len(tokens.in_import) and tokens.in_import[end]:
        end += 1
    assert end - start > 20
    assert region_is_import_only(tokens, start, end)
    # package / `<?php` / trailing statement tokens sit outside the import nodes
    assert ext in ("ts", "tsx") or not region_is_import_only(tokens, 0, len(tokens.hashes))


@pytest.mark.parametrize("ext", _LANGUAGE_EXTENSIONS)
def test_a_real_file_is_not_import_only(ext: str) -> None:
    _, tokens = _index(f"original.{ext}")
    assert not region_is_import_only(tokens, 0, len(tokens.hashes))
    assert not region_is_import_only(tokens, 0, 0)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/billing/invoice_test.go", True),
        ("src/billing/invoice.test.ts", True),
        ("src/billing/invoice.spec.tsx", True),
        ("test/helpers.php", True),
        ("app/tests/Unit/InvoiceTest.php", True),
        ("web/__tests__/invoice.ts", True),
        ("src/billing/invoice.ts", False),
        ("src/latest/invoice.go", False),
        ("src/billing/contest.go", False),
    ],
)
def test_is_test_path(path: str, expected: bool) -> None:
    assert is_test_path(path) is expected


@pytest.mark.parametrize(
    ("path", "excluded"),
    [
        ("src/billing/invoice.ts", False),
        ("src/billing/invoice.php", False),
        ("src/billing/invoice.go", False),
        ("src/billing/invoice_test.go", True),
        ("node_modules/pkg/index.ts", True),
        ("go.sum", True),
        ("src/billing/notes.py", True),
        ("src/billing/invoice.md", True),
    ],
)
def test_is_excluded_path(path: str, excluded: bool) -> None:
    assert is_excluded_path(path) is excluded


# --- US-C9: a window-hash collision can never yield a region --------------


def _synthetic(
    tokens: list[int], file_path: str, *, hash_value: int, offset: int
) -> CloneFileIndex:
    return CloneFileIndex(
        file_path=file_path,
        language="go",
        token_hashes=tuple(tokens),
        line_first_token=(len(tokens),),
        fingerprints=(Fingerprint(hash=hash_value, token_offset=offset),),
    )


def test_a_forced_window_hash_collision_with_unequal_tokens_yields_no_region() -> None:
    query = _synthetic(list(range(200)), "q.go", hash_value=7, offset=10)
    stored = _synthetic(list(range(1000, 1200)), "s.go", hash_value=7, offset=10)
    assert match_regions(query, stored, min_tokens=1, min_lines=1) == ()


def test_a_genuine_hit_is_extended_and_trimmed_to_exact_equality() -> None:
    shared = list(range(500, 650))
    query_tokens = [1, 2, 3, *shared, 9, 9, 9]
    stored_tokens = [4, 5, *shared, 8, 8]
    query = CloneFileIndex(
        file_path="q.go",
        language="go",
        token_hashes=tuple(query_tokens),
        line_first_token=(len(query_tokens),),
        fingerprints=(Fingerprint(hash=11, token_offset=3 + 60),),
    )
    stored = CloneFileIndex(
        file_path="s.go",
        language="go",
        token_hashes=tuple(stored_tokens),
        line_first_token=(len(stored_tokens),),
        fingerprints=(Fingerprint(hash=11, token_offset=2 + 60),),
    )
    (region,) = match_regions(query, stored, min_tokens=1, min_lines=1)
    assert (region.query_token_start, region.query_token_end) == (3, 3 + len(shared))
    assert (region.stored_token_start, region.stored_token_end) == (2, 2 + len(shared))
    assert region.stored_file_path == "s.go"


# --- Winnowing: the no-false-negative guarantee, density, determinism -----


def test_winnowing_never_misses_a_shared_run_of_the_guarantee_length() -> None:
    rng = random.Random(20261017)
    for _ in range(150):
        run_length = rng.randrange(GUARANTEE_TOKENS, GUARANTEE_TOKENS + 200)
        run = [rng.randrange(-(2**31), 2**31) for _ in range(run_length)]
        left_pad = rng.randrange(0, 300)
        right_pad = rng.randrange(0, 300)
        other_left = rng.randrange(0, 300)
        other_right = rng.randrange(0, 300)

        def noise(count: int) -> list[int]:
            return [rng.randrange(-(2**31), 2**31) for _ in range(count)]

        first = [*noise(left_pad), *run, *noise(right_pad)]
        second = [*noise(other_left), *run, *noise(other_right)]
        first_prints = fingerprints(first)
        second_prints = fingerprints(second)
        second_by_hash = {item.hash: item.token_offset for item in second_prints}
        shared = [
            item
            for item in first_prints
            if item.hash in second_by_hash
            and item.token_offset - left_pad == second_by_hash[item.hash] - other_left
            and 0 <= item.token_offset - left_pad <= run_length - GRAM_TOKENS
        ]
        assert shared, f"missed a {run_length}-token run"


def test_match_regions_always_reports_a_shared_run_of_the_guarantee_length() -> None:
    rng = random.Random(20261018)

    def whole_file(tokens: list[int], path: str) -> CloneFileIndex:
        return CloneFileIndex(
            file_path=path,
            language="go",
            token_hashes=tuple(tokens),
            line_first_token=tuple(range(len(tokens))),  # one token per line
            fingerprints=fingerprints(tokens),
        )

    for _ in range(50):
        run_length = rng.randrange(GUARANTEE_TOKENS, GUARANTEE_TOKENS + 200)
        run = [rng.randrange(-(2**31), 2**31) for _ in range(run_length)]
        left, other_left = rng.randrange(0, 300), rng.randrange(0, 300)

        def noise(count: int) -> list[int]:
            return [rng.randrange(-(2**31), 2**31) for _ in range(count)]

        first = [*noise(left), *run, *noise(rng.randrange(0, 300))]
        second = [*noise(other_left), *run, *noise(rng.randrange(0, 300))]
        regions = match_regions(whole_file(first, "q.go"), whole_file(second, "s.go"))
        assert any(
            r.query_token_start <= left and r.query_token_end >= left + run_length for r in regions
        ), f"missed a {run_length}-token run"


def test_a_stream_shorter_than_the_guarantee_has_no_fingerprints() -> None:
    assert fingerprints(list(range(GUARANTEE_TOKENS - 1))) == ()
    assert fingerprints(list(range(GUARANTEE_TOKENS))) != ()


def test_fingerprint_density_is_close_to_two_over_window_plus_one() -> None:
    rng = random.Random(7)
    tokens = [rng.randrange(-(2**31), 2**31) for _ in range(40_000)]
    density = len(fingerprints(tokens)) / len(tokens)
    assert 0.5 * (2 / (WINDOW + 1)) < density < 1.6 * (2 / (WINDOW + 1))


def test_fingerprints_fit_a_postgres_bigint_and_are_deterministic() -> None:
    tokens = list(range(-50, 400))
    first = fingerprints(tokens)
    assert first == fingerprints(tokens)
    assert all(0 <= item.hash < 2**63 for item in first)


_GOLDEN_SCRIPT = (
    "import json; from indexing.clone import fingerprints; "
    "print(json.dumps([[f.hash, f.token_offset] for f in fingerprints(list(range(-50, 400)))]))"
)


def test_fingerprints_are_identical_in_a_separate_process() -> None:
    expected = [[item.hash, item.token_offset] for item in fingerprints(list(range(-50, 400)))]
    env = {**os.environ, "PYTHONHASHSEED": "12345"}
    completed = subprocess.run(
        [sys.executable, "-c", _GOLDEN_SCRIPT],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        cwd=Path(__file__).parent.parent,
    )
    assert json.loads(completed.stdout) == expected


def test_token_hashes_are_signed_32_bit_crc32_of_the_token_text() -> None:
    tokens = tokenize("go", "x.go", parse_file("go", "x.go", b"package p\nfunc f() {}\n"))
    assert tokens.hashes[0] == zlib.crc32(b"package") - (
        1 << 32 if zlib.crc32(b"package") >= 1 << 31 else 0
    )
    assert all(-(2**31) <= value < 2**31 for value in tokens.hashes)


# --- Line map and index shape ---------------------------------------------


def test_line_map_recovers_each_tokens_line() -> None:
    index, tokens = _index("original.go")
    assert len(index.token_hashes) == len(tokens.lines)
    for token_number, line in enumerate(tokens.lines):
        assert clone._line_of(index, token_number) == line


def test_an_empty_file_has_an_empty_index() -> None:
    index, tokens = build_clone_index("go", "e.go", parse_file("go", "e.go", b""))
    assert index.token_hashes == ()
    assert index.line_first_token == ()
    assert index.fingerprints == ()
    assert tokens.hashes == ()


def test_matching_against_a_file_with_no_fingerprints_yields_no_regions() -> None:
    empty, _ = build_clone_index("go", "e.go", parse_file("go", "e.go", b""))
    original, _ = _index("original.go")
    assert match_regions(original, empty) == ()
    assert match_regions(empty, original) == ()


def test_an_unexpected_tokenizer_failure_is_wrapped_as_clone_index_error() -> None:
    class _BrokenTree:
        @property
        def root_node(self) -> object:
            raise RuntimeError("boom")

    with pytest.raises(CloneIndexError) as caught:
        tokenize("go", "x.go", _BrokenTree())  # type: ignore[arg-type]
    assert caught.value.file_path == "x.go" and "boom" in caught.value.detail


# --- Tokenizer completeness (plan Assumption 16) --------------------------


@pytest.mark.parametrize(
    "name",
    sorted(path.name for path in _TESTDATA.iterdir() if path.is_file()),
)
def test_every_fixture_tokenizes_without_an_unclassified_leaf(name: str) -> None:
    index, tokens = _index(name)
    assert index.token_hashes, name
    assert len(tokens.hashes) == len(tokens.lines) == len(tokens.in_import)


def test_an_unclassified_named_leaf_raises_rather_than_normalizing_wrongly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(clone._IDENTIFIER_TYPES, "go", frozenset())
    with pytest.raises(CloneIndexError) as caught:
        tokenize("go", "x.go", parse_file("go", "x.go", b"package p\nfunc f() {}\n"))
    assert caught.value.file_path == "x.go"
    assert "unclassified" in caught.value.detail


# --- US-C7 literal-changed copies; US-C8 merging; golden values -----------


@pytest.mark.parametrize("ext", _LANGUAGE_EXTENSIONS)
def test_a_literal_changed_copy_matches_the_original_end_to_end(ext: str) -> None:
    original, _ = _index(f"original.{ext}")
    regions = _regions(f"literals.{ext}", f"original.{ext}")
    assert [region.token_count for region in regions] == [len(original.token_hashes)]


def test_a_self_similar_file_yields_disjoint_merged_regions() -> None:
    rng = random.Random(3)
    unit = [rng.randrange(-(2**31), 2**31) for _ in range(60)]
    tokens = tuple(unit * 8)
    index = CloneFileIndex(
        file_path="r.go",
        language="go",
        token_hashes=tokens,
        line_first_token=tuple(range(1, len(tokens) + 1)),
        fingerprints=fingerprints(tokens),
    )
    regions = match_regions(index, index, min_tokens=1, min_lines=1)
    assert regions
    for first, second in pairwise(regions):
        assert first.query_token_end < second.query_token_start
    covered = {t for r in regions for t in range(r.query_token_start, r.query_token_end)}
    assert covered == set(range(len(tokens)))


def test_regions_that_touch_on_one_side_only_are_not_merged() -> None:
    assert clone._merge_spans([(0, 10, 0, 10), (10, 20, 50, 60)]) == [
        (0, 10, 0, 10),
        (10, 20, 50, 60),
    ]
    assert clone._merge_spans([(0, 10, 0, 10), (10, 20, 10, 20), (5, 12, 5, 12)]) == [
        (0, 20, 0, 20)
    ]


def test_fingerprint_golden_values_pin_the_stored_hash_scheme() -> None:
    # Changing the modulus, base or gram hash silently invalidates every stored
    # fingerprint: bump CLONE_FINGERPRINT_VERSION and update these together.
    prints = fingerprints(list(range(-50, 400)))
    assert len(prints) == 42
    assert [(item.hash, item.token_offset) for item in prints[:4]] == [
        (79327546158135421, 50),
        (101897352351265038, 52),
        (124467158544394655, 54),
        (147036964737524272, 56),
    ]
    assert (prints[-1].hash, prints[-1].token_offset) == (17318864403601853, 351)


@pytest.mark.parametrize(
    ("language", "path", "source", "expected"),
    [
        (
            "typescript",
            "a.ts",
            b'const x = foo("a", 1);\n',
            (
                1791386863,
                -1353996073,
                -1972722788,
                -1353996073,
                -407419017,
                360303129,
                -522550418,
                360303129,
                -1867483167,
                1661535913,
            ),
        ),
        (
            "tsx",
            "a.tsx",
            b'const x = <div a="b">{y}</div>;\n',
            (
                1791386863,
                -1353996073,
                -1972722788,
                -43150582,
                -1353996073,
                -1353996073,
                -1972722788,
                360303129,
                325317158,
                366298937,
                -1353996073,
                -55123444,
                -1711581607,
                -1353996073,
                325317158,
                1661535913,
            ),
        ),
        (
            "php",
            "a.php",
            b'<?php $x = foo("a", 1);\n',
            (
                -1553222994,
                -301921444,
                -1353996073,
                -1972722788,
                -1353996073,
                -407419017,
                360303129,
                -522550418,
                360303129,
                -1867483167,
                1661535913,
            ),
        ),
        (
            "go",
            "a.go",
            b'package p\nfunc f() { x := g("a", 1) }\n',
            (
                -563583083,
                -1353996073,
                1123462067,
                -1353996073,
                -407419017,
                -1867483167,
                366298937,
                -1353996073,
                1008238743,
                -1353996073,
                -407419017,
                360303129,
                -522550418,
                360303129,
                -1867483167,
                -55123444,
            ),
        ),
    ],
)
def test_token_hashes_have_golden_values_per_language(
    language: ChunkLanguage, path: str, source: bytes, expected: tuple[int, ...]
) -> None:
    assert tokenize(language, path, parse_file(language, path, source)).hashes == expected
