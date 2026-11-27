"""The labelled eval corpus: candidate findings distilled from
docs/research/pr-review-bot-comments-chaptered.md.

Rows are paraphrased, minimal, synthetic excerpts: no secrets, no
verbatim source. ``diff_excerpt`` is ``None`` for rows that only feed the
verification judge (the finding is replayed without a generation call).
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from findings.models import Severity

EVAL_DIR = Path(__file__).resolve().parent.parent / "testdata" / "eval"
CORPUS_PATH = EVAL_DIR / "corpus.jsonl"


class EvalRow(BaseModel):
    """One labelled candidate finding. ``(file, line)`` is unique across the
    corpus and is how a kept ``Finding`` is matched back to its row.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    source_pr: int
    file: str
    line: int
    severity: Severity
    finding_text: str
    label: Literal["true_positive", "noise"]
    category: str
    diff_excerpt: str | None


def load_corpus(path: Path = CORPUS_PATH) -> list[EvalRow]:
    return [
        EvalRow.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()
    ]


def group_by_pr(rows: list[EvalRow]) -> list[tuple[int, list[EvalRow]]]:
    """Rows grouped per source PR (one eval "PR"), PRs in ascending order."""
    groups: dict[int, list[EvalRow]] = defaultdict(list)
    for row in rows:
        groups[row.source_pr].append(row)
    return sorted(groups.items())
