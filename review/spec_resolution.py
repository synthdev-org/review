"""review.spec_resolution — find the spec/PRD document a PR implements.

Pure matching (``tokenize``, ``is_spec_like``, ``_match``) plus the thin
async ``resolve_spec`` orchestration over the existing ``DiffFetcher`` and
``RepoTreeFetcher`` Protocols. Internal helper of the ``spec_review`` macro step;
defines no ``Step`` of its own.

The typed fetch errors (``DiffContentUnavailableError``,
``RepoTreeFetchError``, ``RepoTreeTruncatedError``,
``RepoTreeContentFetchError``) propagate to the caller, which degrades.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from indexing.tree import RepoTreeFetcher
from ingestion.diff import (
    Diff,
    DiffContentMalformedError,
    DiffContentUnavailableError,
    DiffFetcher,
)
from review.diff_prompt import INERT_REFERENCE_DATA_FRAMING

MAX_SPEC_CHARS = 20_000

_STOPLIST = frozenset(
    {
        "feat",
        "feature",
        "fix",
        "chore",
        "bug",
        "hotfix",
        "docs",
        "spec",
        "specs",
        "prd",
        "md",
        "main",
        "wip",
        # Generic verbs that appear in many branch names and say nothing about
        # which spec a PR implements.
        "add",
        "update",
        "new",
        "refactor",
        "implement",
        "improve",
        "cleanup",
        "change",
        "changes",
        "test",
        "tests",
    }
)
_SPEC_DIRS = ("docs/", "specs/", ".scratch/")
# Whole-token match on the name, not a substring: "inspection-guide.md" must
# not count as a spec just because it contains "spec".
# A spec lives in a spec-named directory or *starts* with a spec token
# ("spec-foo.md", "prd-bar.md"); "plan-x-spec-review.md" is a plan.
_SPEC_NAME_TOKENS = frozenset({"spec", "specs", "prd", "prds"})
_SPLIT = re.compile(r"[/\-_.\s]+")

SpecOutcome = Literal["found_in_diff", "found_in_repo", "none", "ambiguous"]


class ResolvedSpec(BaseModel):
    """The spec document chosen for a PR, already decoded and size-capped."""

    model_config = ConfigDict(frozen=True)

    path: str
    text: str
    source: Literal["diff", "repo"]
    truncated: bool


class SpecResolution(BaseModel):
    """``spec`` is set only for the two ``found_*`` outcomes."""

    model_config = ConfigDict(frozen=True)

    spec: ResolvedSpec | None
    outcome: SpecOutcome


def tokenize(value: str) -> frozenset[str]:
    """Lower-cased distinct tokens, minus the stoplist and pure-digit tokens."""
    return frozenset(
        token
        for token in _SPLIT.split(value.lower())
        if token and token not in _STOPLIST and not token.isdigit()
    )


def is_spec_like(path: str) -> bool:
    lowered = path.lower()
    if not (lowered.endswith(".md") and lowered.startswith(_SPEC_DIRS)):
        return False
    *directories, name = lowered.split("/")
    first_name_token = next((t for t in _SPLIT.split(name) if t), "")
    return first_name_token in _SPEC_NAME_TOKENS or any(
        directory in _SPEC_NAME_TOKENS for directory in directories
    )


def _match(
    branch_ref: str | None, candidate_paths: Sequence[str]
) -> tuple[str | None, Literal["found", "none", "ambiguous"]]:
    branch_tokens = tokenize(branch_ref or "")
    if not branch_tokens:
        return None, "none"
    needed = 1 if len(branch_tokens) == 1 else 2
    scored = [
        (len(branch_tokens & tokenize(path)), path)
        for path in candidate_paths
        if is_spec_like(path)
    ]
    qualifying = [(score, path) for score, path in scored if score >= needed]
    if not qualifying:
        return None, "none"
    best = max(score for score, _ in qualifying)
    winners = {path for score, path in qualifying if score == best}
    if len(winners) != 1:
        return None, "ambiguous"
    return winners.pop(), "found"


def render_spec_section(spec: ResolvedSpec) -> str:
    """The labelled, fenced prompt section carrying untrusted spec text (US-S11),
    shared by ``spec_review`` and the verifier so both frame it identically.
    The fence is lengthened until it cannot occur inside the text, so the
    spec cannot close its own block early."""
    fence = "=====SPEC"
    while fence in spec.text:
        fence += "="
    # Git paths may hold newlines or control characters; keep the label line
    # on one line so a crafted filename cannot add text outside the fence.
    safe_path = re.sub(r"[^\x20-\x7e]", "?", spec.path)
    return (
        f"Spec the PR implements (path: {safe_path}; {INERT_REFERENCE_DATA_FRAMING}):\n"
        f"{fence} BEGIN\n{spec.text}\n{fence} END"
    )


def _to_spec(path: str, raw: bytes, source: Literal["diff", "repo"]) -> ResolvedSpec:
    text = raw.decode("utf-8", errors="replace")
    return ResolvedSpec(
        path=path,
        text=text[:MAX_SPEC_CHARS],
        source=source,
        truncated=len(text) > MAX_SPEC_CHARS,
    )


async def resolve_spec(
    *,
    diff_fetcher: DiffFetcher,
    tree_fetcher: RepoTreeFetcher,
    installation_id: int,
    repo_full_name: str,
    head_sha: str,
    pr_head_ref: str | None,
    diff: Diff,
) -> SpecResolution:
    """Diff-added/modified specs win; otherwise the repo tree at ``head_sha``."""
    # Guard before any I/O: a recursive tree listing must not be paid when
    # the branch name cannot match anything.
    if not tokenize(pr_head_ref or ""):
        return SpecResolution(spec=None, outcome="none")

    diff_paths = [f.path for f in diff.files if f.status != "removed"]
    path, verdict = _match(pr_head_ref, diff_paths)
    if verdict == "ambiguous":
        # A tie inside the diff is a tie: never let the repo tree break it.
        return SpecResolution(spec=None, outcome="ambiguous")
    if path is not None:
        try:
            content = await diff_fetcher.fetch_content(
                installation_id=installation_id,
                repo_full_name=repo_full_name,
                diff=diff,
                paths=[path],
            )
        except (DiffContentUnavailableError, DiffContentMalformedError):
            # An oversized/unreadable diff blob must not hide a readable copy
            # of the same spec in the repo tree.
            content = {}
        if path in content:
            return SpecResolution(
                spec=_to_spec(path, content[path], "diff"), outcome="found_in_diff"
            )
        # The diff's copy could not be read (skipped blob, symlink, ...): fall
        # through to the repo tree rather than silently skipping the check.

    tree = await tree_fetcher.fetch_tree(
        installation_id=installation_id, repo_full_name=repo_full_name, commit_sha=head_sha
    )
    path, verdict = _match(pr_head_ref, [entry.path for entry in tree])
    if path is None:
        return SpecResolution(spec=None, outcome="ambiguous" if verdict == "ambiguous" else "none")
    entry = next(e for e in tree if e.path == path)
    content = await tree_fetcher.fetch_content(
        installation_id=installation_id,
        repo_full_name=repo_full_name,
        paths_with_blob_sha=[entry],
    )
    if path not in content:
        return SpecResolution(spec=None, outcome="none")
    return SpecResolution(spec=_to_spec(path, content[path], "repo"), outcome="found_in_repo")
