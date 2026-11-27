"""Unit tests for the committed eval corpus's invariants."""

from __future__ import annotations

from collections import Counter

from review.eval.corpus import group_by_pr, load_corpus


def test_corpus_has_the_planned_shape() -> None:
    rows = load_corpus()
    labels = Counter(r.label for r in rows)

    assert labels == {"true_positive": 40, "noise": 25}
    assert any(r.diff_excerpt is None for r in rows)  # verification-only rows exist


def test_rows_are_uniquely_addressable() -> None:
    rows = load_corpus()

    assert len({r.id for r in rows}) == len(rows)
    assert len({(r.file, r.line) for r in rows}) == len(rows)
    assert len({r.finding_text for r in rows}) == len(rows)  # the synthetic judge keys on text


def test_each_pr_has_at_most_one_diff_row_per_file() -> None:
    for pr, rows in group_by_pr(load_corpus()):
        files = [r.file for r in rows if r.diff_excerpt is not None]
        assert len(files) == len(set(files)), f"PR {pr} reuses a file across diff rows"
