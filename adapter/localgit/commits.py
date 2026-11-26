"""`LocalGitCommitSource` — implements `ingestion.commits.CommitSource` over a
local git repository (`git log` / `git diff`). One instance covers one
`base_ref..head_ref` range."""

from __future__ import annotations

from pathlib import Path

from adapter.localgit.diff import LocalGitDiffFetcher, LocalGitError, run_git
from ingestion.commits import CommitInfo, CommitsUnavailableError
from ingestion.diff import Diff

_FIELD_SEP = "\x1f"


class LocalGitCommitSource:
    def __init__(self, repo: Path, base_ref: str, head_ref: str) -> None:
        self._repo = repo
        self._range = f"{base_ref}..{head_ref}"

    async def list_commits(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> tuple[CommitInfo, ...]:
        try:
            out = await run_git(
                self._repo,
                "log",
                "--reverse",
                "--topo-order",
                f"--format=%H{_FIELD_SEP}%P{_FIELD_SEP}%s",
                "--end-of-options",
                self._range,
            )
        except LocalGitError as exc:
            raise CommitsUnavailableError(repo_full_name=repo_full_name, detail=str(exc)) from exc
        commits: list[CommitInfo] = []
        for row in out.decode(errors="replace").splitlines():
            sha, parents, title = row.split(_FIELD_SEP, 2)
            commits.append(CommitInfo(sha=sha, parent_shas=tuple(parents.split()), title=title))
        return tuple(commits)

    async def fetch_range_diff(
        self, *, installation_id: int, repo_full_name: str, base_sha: str, head_sha: str
    ) -> Diff:
        try:
            return await LocalGitDiffFetcher(self._repo, base_sha, head_sha).fetch(
                installation_id=installation_id,
                repo_full_name=repo_full_name,
                pr_number=0,
                head_sha=head_sha,
            )
        except (LocalGitError, IndexError, ValueError) as exc:
            raise CommitsUnavailableError(repo_full_name=repo_full_name, detail=str(exc)) from exc
