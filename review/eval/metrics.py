"""Eval metrics over per-row outcomes. Pure; no I/O."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from pydantic import BaseModel

from review.eval.corpus import EvalRow


@dataclass(frozen=True)
class Outcome:
    row: EvalRow
    kept: bool


class Metrics(BaseModel):
    """``precision`` is 1.0 when nothing was kept (no wrong finding posted);
    ``recall`` is 1.0 when the corpus has no true positives. Floats are
    rounded to 4 places so the committed baseline compares exactly."""

    precision: float
    recall: float
    findings_per_pr: float
    keep_rate_by_category: dict[str, float]
    kept_ids: list[str]


def compute_metrics(outcomes: list[Outcome]) -> Metrics:
    kept = [o for o in outcomes if o.kept]
    true_kept = [o for o in kept if o.row.label == "true_positive"]
    true_total = [o for o in outcomes if o.row.label == "true_positive"]
    pr_count = len({o.row.source_pr for o in outcomes})

    per_category: dict[str, list[bool]] = defaultdict(list)
    for o in outcomes:
        per_category[o.row.category].append(o.kept)

    return Metrics(
        precision=round(len(true_kept) / len(kept), 4) if kept else 1.0,
        recall=round(len(true_kept) / len(true_total), 4) if true_total else 1.0,
        findings_per_pr=round(len(kept) / pr_count, 4) if pr_count else 0.0,
        keep_rate_by_category={
            category: round(sum(flags) / len(flags), 4)
            for category, flags in sorted(per_category.items())
        },
        kept_ids=sorted(o.row.id for o in kept),
    )
