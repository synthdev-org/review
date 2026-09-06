"""ingestion.diff — the ``Diff``/``DiffFile`` data model and the
``DiffFetcher`` Protocol operating over it, per the plan's Phase 4 section.

**Placement — a Protocol defined alongside the data model it produces, not
at its first consumer, per ``docs/adr/0006-protocol-defined-at-data-model.md``:**
``ingestion/diff.py`` isn't itself one of ``DiffFetcher``'s consumers
(``review/prefilter.py`` this phase; ``review/static_analysis.py`` and
``review/llm_review.py`` in later phases are) — it's the package that owns
the ``Diff`` model every consumer receives, mirroring how ``findings/models.py``
owns ``Finding`` for every consumer that produces or reads one. See that
ADR for the general rule this instance follows.

``ingestion`` has zero infra imports (no ``httpx``/``sqlalchemy``/etc.),
per architecture.md's domain-package rule — ``adapter/github/diff.py``
(this phase) is the one concrete implementation, wired in only at the
composition root (``apps/worker/main.py``).

Bare diff only, per the roadmap — no AST parsing, no index.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class DiffFile(BaseModel):
    """One changed file from a PR's diff, per the plan's Phase 4 section.

    ``mode`` is the real octal git mode (``100644``/``100755``/``120000``/
    ``160000``) sourced from the Git Trees API — not a derived value from a
    differently-shaped field — so a symlink (``120000``) or submodule
    (``160000``) entry can be distinguished before any content is fetched
    for it (see ``adapter/github/diff.py``'s materialization-safety guard).
    ``mode``/``blob_sha``/``blob_size_bytes`` are all ``None`` for a
    ``removed`` file — it no longer exists at the head commit's tree (the
    Trees API is fetched against the PR's *head* SHA), so there is no mode
    to resolve, and nothing to fetch or size.
    """

    model_config = ConfigDict(frozen=True)

    path: str
    status: str
    """GitHub's own changed-file status: ``added``/``modified``/``removed``/etc."""
    patch: str | None = None
    """Unified-diff patch text. GitHub omits this for some files (e.g. a
    binary file, or a diff too large to render) — never assumed present."""
    mode: str | None = None
    blob_sha: str | None = None
    blob_size_bytes: int | None = None


class Diff(BaseModel):
    """A PR's full changed-file list at one head commit, per the plan's
    Phase 4 section. ``review/prefilter.py`` (this phase) narrows this to
    the subset that should proceed to static-analysis/LLM review.
    """

    model_config = ConfigDict(frozen=True)

    files: tuple[DiffFile, ...]


class DiffContentUnavailableError(Exception):
    """Raised by ``DiffFetcher.fetch_content`` when real file content could
    not be fetched at all — the GitHub (or other host) API was unreachable,
    its retry budget was exhausted, or a requested blob exceeded the
    adapter's own size ceiling.

    Domain-owned, defined alongside the ``DiffFetcher`` Protocol it's part
    of the failure contract for — mirroring ``indexing/tree.py``'s existing
    ``RepoTreeFetchError``/``RepoTreeTruncatedError`` pattern (a domain
    package's own typed exceptions for an adapter-implemented Protocol's
    failure modes) — so a domain-package caller (Phase 5's
    ``ContextBuilderStep``) can catch this for graceful degradation (US7)
    without importing an adapter-owned exception type by name, which would
    violate architecture.md's "domain never imports adapter code" rule. The
    concrete GitHub-backed implementation
    (``adapter/github/diff.py``'s ``GitHubDiffFetcher.fetch_content``)
    translates its own adapter-level exceptions into this one at its own
    call sites.
    """

    def __init__(self, *, repo_full_name: str, detail: str) -> None:
        super().__init__(f"content fetch for {repo_full_name} unavailable: {detail}")
        self.repo_full_name = repo_full_name
        self.detail = detail


class DiffContentMalformedError(Exception):
    """Raised by ``DiffFetcher.fetch_content`` when the underlying API
    responded, but its response could not be parsed into the content this
    Protocol promises — see ``DiffContentUnavailableError``'s own docstring
    for the placement/translation rationale, identical here.
    """

    def __init__(self, *, repo_full_name: str, detail: str) -> None:
        super().__init__(f"content fetch for {repo_full_name} returned malformed data: {detail}")
        self.repo_full_name = repo_full_name
        self.detail = detail


class DiffFetcher(Protocol):
    """Fetches a PR's diff and, for a narrowed subset of paths, real file
    content — per the plan's Phase 4 section. Implemented by
    ``adapter/github/diff.py``'s ``GitHubDiffFetcher``.

    ``fetch`` and ``fetch_content`` are deliberately two separate calls, not
    one: ``review/prefilter.py`` (this phase) calls only ``fetch`` — the
    changed-file list plus per-file mode/size, no content — and
    ``review/static_analysis.py`` (Phase 5) calls ``fetch_content`` only
    after pre-filter has narrowed the path set, so content is never fetched
    for a file pre-filter would have excluded anyway (lockfiles, vendored
    paths, generated files, files beyond the materialization ceiling).
    """

    async def fetch(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> Diff:
        """Fetch every changed file's path/status/patch (paginated
        Files-endpoint) plus real git mode/blob sha/blob size
        (cross-referenced from one Git Trees API call) — no file content.
        """
        ...

    async def fetch_content(
        self,
        *,
        installation_id: int,
        repo_full_name: str,
        diff: Diff,
        paths: Sequence[str],
    ) -> dict[str, bytes]:
        """Fetch real file content for exactly ``paths`` (a subset of
        ``diff.files``, already narrowed by ``review/prefilter.py``) via
        the Git Blobs API, keyed by each entry's own ``blob_sha`` — never by
        path. A symlink (``120000``) or submodule (``160000``) entry in
        ``diff`` is skipped entirely (no blob fetched), even if its path is
        present in ``paths``.

        Raises ``DiffContentUnavailableError``/``DiffContentMalformedError``
        (per docs/plans/plan-deliver-context-builder.md's Phase 1 section) —
        never an adapter-owned exception type — on failure.
        """
        ...
