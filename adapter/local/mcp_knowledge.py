"""adapter.local.mcp_knowledge — the developer's nominated MCP servers as review context (ADR-0017).

Local mode has no code index; its repository and product context comes from MCP servers the
developer already runs (spec D3, US-L9 to US-L11). This module plugs those servers into the
*existing* seam server mode uses for product knowledge: ``ContextBuilderStep``'s provider branch
asks an ``EffectiveProviderResolver`` for a setting, a ``ProviderOpener`` for a
``ProductKnowledgeProvider``, then calls ``negotiate`` / ``search`` / ``fetch``, assembles the items
into a labelled section and frames it with ``INERT_REFERENCE_DATA_FRAMING``. Nothing in
``review/`` changes. The implementations here are new and used only by the local composition root.

How a consultation goes (``McpKnowledgeProvider.search``):

1. The query is **built, never model-written** (``build_query``): changed file paths and up to ten
   added or changed symbol names, from the *redacted* diff. The pipeline's own query (a pull-request
   title, which local mode does not have) is ignored; the seed carries a fixed marker title only so
   the branch does not stop at "nothing to ask".
2. The query goes through input redaction again and is withheld if it changes (a secret in it).
3. Each nominated server is consulted at most once, concurrently, under one overall deadline; a
   server that does not finish in time is cancelled, which kills and reaps its process. A failure
   of one server is recorded and never fails the review (US-L10).
4. Every response text is redacted (one batched scan) before it can enter a prompt, then cut to the
   limits server mode applies (1,000 characters as a snippet, 4,000 for the two best items) with
   the cut recorded. No content is ever read as instructions: there is no second call.
5. ``fetch`` is answered from the texts already held (no second tool call), so the item carries up
   to 4,000 characters while each server is still called once.

``records`` holds what each server did, for the run summary and the dry-run egress report; it has
names, statuses, counts and the (redacted) query, never a response.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from adapter.local.exceptions import RedactionFailedError
from adapter.local.redaction import SecretLineFinder, is_denied_path, redact_texts
from findings.redact import REDACTED_PATH_PREFIX
from ingestion.diff import Diff
from review.product_knowledge import (
    FETCH_CONTENT_MAX_CHARS,
    FETCH_TOP_N,
    SNIPPET_MAX_CHARS,
    TOP_K,
    EffectiveProvider,
    NegotiationResult,
    formulate_query,
)

# The seed's pull-request title: a fixed marker, not a query (see the module docstring).
QUERY_MARKER_TITLE: Final = "local review MCP consultation"
QUERY_MAX_CHARS: Final = 500
MAX_QUERY_SYMBOLS: Final = 10
MAX_QUERY_FILES: Final = 10

ServerStatus = Literal["used", "empty", "timed_out", "errored", "not_consulted"]


# --- the consumer-side Protocol -----------------------------------------------------------------


@dataclass(frozen=True)
class McpAnswer:
    """The text blocks one tool call returned, and the tool that was called."""

    tool: str
    texts: tuple[str, ...]


class McpServerFailure(Exception):
    """A server could not supply context. ``reason`` is a closed phrase (never server text)."""

    def __init__(self, reason: str, *, timed_out: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.timed_out = timed_out


class McpServerClient(Protocol):
    async def consult(self, *, query: str, tool: str | None) -> McpAnswer:
        """Ask the server once. Raises ``McpServerFailure``."""


@dataclass(frozen=True)
class McpServerHandle:
    name: str
    transport: str  # "stdio" | "http"
    host: str | None  # an http server's host (no userinfo, path or query); None for stdio
    tool: str | None  # the nomination's tool, if it named one
    client: McpServerClient


@dataclass
class McpServerRecord:
    """What happened with one nominated server in this run."""

    name: str
    transport: str
    host: str | None
    tool: str | None
    status: ServerStatus = "not_consulted"
    reason: str | None = "the review stopped before it was reached"
    query: str | None = None
    items: int = 0
    truncated_items: int = 0


# --- query --------------------------------------------------------------------------------------

_SYMBOL: Final = re.compile(
    r"^\s*(?:export\s+|default\s+|public\s+|private\s+|protected\s+|static\s+|async\s+|pub\s+)*"
    r"(?:def|class|function|func|fn|interface|struct|enum|trait|type)\s+"
    r"(?:\([^)]*\)\s*)?([A-Za-z_$][\w$]*)"
)
_HUNK: Final = re.compile(r"^@@[^@]*@@(.*)$")
_SAFE_PATH: Final = re.compile(r"[A-Za-z0-9_./-]+")
# A tool name comes from the server: only a plain, short one may reach the item title.
_TOOL_LABEL: Final = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _symbols(patch: str) -> list[str]:
    found: list[str] = []
    for line in patch.split("\n"):
        header = _HUNK.match(line)
        if header:
            text = header.group(1)
        elif line[:1] in ("+", "-", " ") and not line.startswith(("+++", "---")):
            # Added and removed lines name what changed; a context line that declares a symbol
            # names the one the change sits in.
            text = line[1:]
        else:
            continue
        match = _SYMBOL.match(text)
        if match:
            found.append(match.group(1))
    return found


def _short_path(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[-2:])


def build_query(diff: Diff) -> str | None:
    """``"<parent/basename of each changed file> <up to 10 symbol names>"``, at most 500
    characters; ``None`` when the diff names nothing worth asking about. Paths that were redacted
    or deny-listed are left out. Deterministic: the same diff gives the same query."""
    paths: list[str] = []
    symbols: list[str] = []
    for file in diff.files:
        if file.path.startswith(REDACTED_PATH_PREFIX) or is_denied_path(file.path):
            continue
        short = _short_path(file.path)
        # A PR author controls file names; only a plain path charset may reach a server's query.
        if not _SAFE_PATH.fullmatch(short):
            continue
        if short not in paths and len(paths) < MAX_QUERY_FILES:
            paths.append(short)
        if file.patch:
            for name in _symbols(file.patch):
                if name not in symbols:
                    symbols.append(name)
    words = [*paths, *symbols[:MAX_QUERY_SYMBOLS]]
    if not words:
        return None
    query = ""
    for word in words:
        candidate = f"{query} {word}".strip()
        if len(candidate) > QUERY_MAX_CHARS:
            break
        query = candidate
    return formulate_query(query, ())


# --- the provider -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    server: str
    tool: str
    text: str


def _source_type(server: str) -> str:
    slug = server.lower()[:32]
    return slug if re.fullmatch(r"[a-z][a-z0-9_.-]{0,31}", slug) else "other"


class McpKnowledgeProvider:
    """``review.product_knowledge.ProductKnowledgeProvider`` over the nominated servers.

    ``consult=False`` is the ``--dry-run`` mode: the query is built and recorded exactly as a real
    run builds it, and nothing is started or contacted.
    """

    def __init__(
        self,
        handles: Sequence[McpServerHandle],
        *,
        diff: Callable[[], Diff | None],
        finder: SecretLineFinder,
        deadline_seconds: float,
        max_calls: int,
        consult: bool,
    ) -> None:
        self._handles = tuple(handles)
        self._diff = diff
        self._finder = finder
        self._deadline = deadline_seconds
        self._max_calls = max_calls
        self._consult = consult
        self.records: dict[str, McpServerRecord] = {
            h.name: McpServerRecord(
                name=h.name,
                transport=h.transport,
                host=h.host,
                tool=h.tool,
                status="not_consulted",
                reason="dry run" if not consult else "the review stopped before it was reached",
            )
            for h in handles
        }
        # Item order as the context section will number them; the server that supplied each.
        self.supplied_by_rank: list[str] = []
        self._full: dict[str, str] = {}

    # --- ProductKnowledgeProvider -----------------------------------------------------------

    async def negotiate(self) -> NegotiationResult:
        # Local servers speak no product-knowledge contract; "1" only satisfies the branch.
        return NegotiationResult(chosen_version="1", supported_versions=["1"], tools_present=[])

    async def search(self, query: str) -> object:
        built = build_query(diff) if (diff := self._diff()) is not None else None
        if built is None:
            self._settle_all("empty", "nothing in the diff to ask about")
            return {"results": []}
        try:
            (screened,), _ = await redact_texts(self._finder, [built])
        except RedactionFailedError:
            self._settle_all("errored", "redaction failed, nothing sent")
            return {"results": []}
        if screened != built:
            self._settle_all("errored", "query withheld: it held a secret")
            return {"results": []}
        for record in self.records.values():
            record.query = screened
        if not self._consult:
            return {"results": []}
        answers = await self._ask_all(screened)
        return await self._results(answers)

    async def fetch(self, item_id: str) -> object:
        text = self._full.get(item_id)
        return {"content": text[:FETCH_CONTENT_MAX_CHARS]} if text is not None else {}

    # --- internals --------------------------------------------------------------------------

    def _settle_all(self, status: ServerStatus, reason: str) -> None:
        for record in self.records.values():
            record.status, record.reason = status, reason

    async def _ask_one(self, handle: McpServerHandle, query: str) -> McpAnswer | None:
        record = self.records[handle.name]
        try:
            answer = await handle.client.consult(query=query, tool=handle.tool)
        except McpServerFailure as exc:
            record.status = "timed_out" if exc.timed_out else "errored"
            record.reason = exc.reason
            return None
        return answer

    async def _ask_all(self, query: str) -> dict[str, McpAnswer]:
        """Every server at once under one deadline. A server still running at the deadline, or when
        this coroutine is cancelled, is cancelled and awaited so its process is killed and reaped
        before anything else happens."""
        handles = self._handles[: self._max_calls]
        for handle in self._handles[self._max_calls :]:
            self.records[handle.name].status = "errored"
            self.records[handle.name].reason = "over the call cap"
        tasks = {h.name: asyncio.ensure_future(self._ask_one(h, query)) for h in handles}
        if not tasks:
            return {}
        try:
            await asyncio.wait(tasks.values(), timeout=self._deadline)
        finally:
            for name, task in tasks.items():
                if not task.done():
                    task.cancel()
                    self.records[name].status = "timed_out"
                    self.records[name].reason = "timeout"
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        answers: dict[str, McpAnswer] = {}
        for name, task in tasks.items():
            if task.cancelled():
                continue
            if task.exception() is not None:
                # Not an McpServerFailure: record it, never leave the "not consulted" default.
                self.records[name].status = "errored"
                self.records[name].reason = "failed unexpectedly"
                continue
            answer = task.result()
            if answer is not None:
                answers[name] = answer
        return answers

    async def _results(self, answers: dict[str, McpAnswer]) -> object:
        pending = [
            (name, answer.tool if _TOOL_LABEL.fullmatch(answer.tool) else "tool", text)
            for name, answer in answers.items()
            for text in answer.texts
            if text.strip()
        ]
        for name in answers:
            if not any(n == name for n, _, _ in pending):
                self.records[name].status, self.records[name].reason = "empty", None
        if not pending:
            return {"results": []}
        try:
            # The tool labels go through the same scan as the texts (one batch).
            redacted, _ = await redact_texts(
                self._finder, [*(text for _, _, text in pending), *(tool for _, tool, _ in pending)]
            )
            texts, labels = redacted[: len(pending)], redacted[len(pending) :]
        except RedactionFailedError:
            for name in answers:
                self.records[name].status = "errored"
                self.records[name].reason = "redaction failed, nothing used"
            return {"results": []}
        items = [
            _Item(name, label, text)
            for (name, _, _), label, text in zip(pending, labels, texts, strict=True)
            if text.strip()
        ][:TOP_K]
        results: list[dict[str, object]] = []
        for rank, item in enumerate(items):
            cap = FETCH_CONTENT_MAX_CHARS if rank < FETCH_TOP_N else SNIPPET_MAX_CHARS
            record = self.records[item.server]
            record.status, record.reason = "used", None
            record.items += 1
            if len(item.text) > cap:
                record.truncated_items += 1
            ident = f"{item.server}:{rank}"
            self._full[ident] = item.text
            self.supplied_by_rank.append(item.server)
            results.append(
                {
                    "id": ident,
                    "title": f"{item.server}: {item.tool} result {record.items}",
                    "snippet": item.text[:SNIPPET_MAX_CHARS],
                    "source_type": _source_type(item.server),
                }
            )
        for name in answers:
            if self.records[name].status not in ("used", "empty"):
                # It answered, but the item cap left none of its results in the prompt.
                self.records[name].status = "empty"
                self.records[name].reason = "its results were over the item cap"
        return {"results": results}

    def suppliers(self, ref_ids: Sequence[int], *, first_id: int) -> tuple[str, ...]:
        """The servers whose items the ids name, in order, once each. Item ids continue after the
        last code item (``first_id``), best item first."""
        names: list[str] = []
        for ref in ref_ids:
            rank = ref - first_id
            if 0 <= rank < len(self.supplied_by_rank):
                name = self.supplied_by_rank[rank]
                if name not in names:
                    names.append(name)
        return tuple(names)


# --- the seam implementations -------------------------------------------------------------------


@dataclass
class LocalMcpResolver:
    """``EffectiveProviderResolver``: the nominated servers are one provider, always configured.

    ``allow_public_repos`` is deliberate: server mode's exposure gate stops a *tenant-configured*
    provider from receiving a public repository's or a fork's content. Here the developer (or the
    workflow author) named the server and reads the exact query in ``--dry-run``.
    """

    label: str
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def resolve(self, org_id: str, repo_id: str) -> EffectiveProvider | None:
        self.calls.append((org_id, repo_id))
        return EffectiveProvider(
            display_name=self.label, endpoint_url="mcp://local", allow_public_repos=True
        )


@dataclass(frozen=True)
class LocalMcpOpener:
    """``ProviderOpener``: hands the branch the one provider."""

    provider: McpKnowledgeProvider

    def __call__(self, endpoint_url: str, credential_ref: str | None) -> McpKnowledgeProvider:
        return self.provider
