"""Unit tests for review.eval.metrics."""

from __future__ import annotations

from review.eval.corpus import EvalRow
from review.eval.metrics import Outcome, compute_metrics


def _row(row_id: str, *, label: str, category: str, pr: int = 1) -> EvalRow:
    return EvalRow.model_validate(
        {
            "id": row_id,
            "source_pr": pr,
            "file": f"{row_id}.py",
            "line": 1,
            "severity": "medium",
            "finding_text": row_id,
            "label": label,
            "category": category,
            "diff_excerpt": None,
        }
    )


def test_metrics_precision_recall_per_pr_and_category() -> None:
    outcomes = [
        Outcome(_row("tp1", label="true_positive", category="security"), kept=True),
        Outcome(_row("tp2", label="true_positive", category="security", pr=2), kept=False),
        Outcome(_row("n1", label="noise", category="style"), kept=True),
        Outcome(_row("n2", label="noise", category="style", pr=2), kept=False),
    ]

    metrics = compute_metrics(outcomes)

    assert metrics.precision == 0.5
    assert metrics.recall == 0.5
    assert metrics.findings_per_pr == 1.0
    assert metrics.keep_rate_by_category == {"security": 0.5, "style": 0.5}
    assert metrics.kept_ids == ["n1", "tp1"]


def test_metrics_with_nothing_kept_has_perfect_precision_and_zero_recall() -> None:
    metrics = compute_metrics(
        [Outcome(_row("tp1", label="true_positive", category="x"), kept=False)]
    )

    assert metrics.precision == 1.0
    assert metrics.recall == 0.0
    assert metrics.findings_per_pr == 0.0
