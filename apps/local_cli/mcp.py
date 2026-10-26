"""The local CLI's wiring for MCP knowledge (US-L9 to US-L11, ADR-0017).

Composition only: it turns the nominations and the developer's configuration
(``adapter.local.mcp_config``) into clients (``adapter.product_knowledge.mcp_tools``) behind the
consumer-side Protocol ``adapter.local.mcp_knowledge`` defines, and into the provider the shared
pipeline's product-knowledge seam takes. Nothing here runs unless a server is nominated.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from adapter.local.mcp_config import (
    McpConfigError,
    Nomination,
    ResolvedServer,
    load_nomination_file,
    merge_nominations,
    parse_option,
    resolve_servers,
    url_host,
)
from adapter.local.mcp_knowledge import (
    LocalMcpOpener,
    LocalMcpResolver,
    McpAnswer,
    McpKnowledgeProvider,
    McpServerFailure,
    McpServerHandle,
)
from adapter.local.redaction import SecretLineFinder
from adapter.local.summary import McpServerOutcome
from adapter.product_knowledge.client import ClientTimeouts
from adapter.product_knowledge.mcp_tools import (
    HttpTarget,
    McpConsultError,
    McpFailure,
    McpLimits,
    McpToolClient,
    StdioRuntime,
    StdioTarget,
)
from apps.local_cli.settings import LocalSettings
from findings.models import Finding
from ingestion.diff import Diff
from review.context_bundle import ContextBundle


class _ToolClient:
    """``McpServerClient`` over the SDK-backed client: maps its closed failures across."""

    def __init__(self, inner: McpToolClient, *, tool_trusted: bool) -> None:
        self._inner = inner
        # True only when the developer named the tool with --mcp NAME:TOOL; a tool named by a
        # repository file still has to be declared read-only by the server.
        self._tool_trusted = tool_trusted

    async def consult(self, *, query: str, tool: str | None) -> McpAnswer:
        try:
            consulted = await self._inner.consult(
                query=query, tool=tool, tool_trusted=self._tool_trusted
            )
        except McpConsultError as exc:
            raise McpServerFailure(
                exc.failure.value, timed_out=exc.failure is McpFailure.TIMEOUT
            ) from exc
        return McpAnswer(tool=consulted.tool, texts=consulted.texts)


async def resolve_nominated(
    settings: LocalSettings,
    *,
    repo_dir: Path,
    home: Path,
    options: Sequence[str],
    in_ci: bool,
    base_commit: str,
    staged: bool = False,
    environ: Mapping[str, str],
    variables: Mapping[str, str] | None = None,
) -> list[ResolvedServer]:
    """The nominated servers with their definitions, or ``[]`` (and no file read beyond the
    nomination file) when none is nominated. In CI the nomination file is read from the base
    commit. With --staged the base commit is HEAD, which a pull request controls, so
    in CI a nomination file there is refused. Raises ``McpConfigError`` (exit status 2) for
    anything unusable."""
    from_option = tuple(parse_option(o) for o in options)
    from_file = await load_nomination_file(
        repo_dir,
        settings.mcp_nominations_file,
        base_commit=base_commit if in_ci else None,
        environ=environ,
    )
    if in_ci and staged and from_file:
        raise McpConfigError(
            f"{settings.mcp_nominations_file}: CI was detected with --staged, so the file would be "
            "read from the pull-request head, which can nominate its own servers. Use --base in "
            "CI, or name the servers with --mcp"
        )
    nominations: tuple[Nomination, ...] = merge_nominations(from_option, from_file)
    # Blocking file reads (~/.claude.json can be large): off the event loop.
    return await asyncio.to_thread(
        resolve_servers,
        nominations,
        repo_dir=repo_dir,
        home=home,
        explicit_config=settings.mcp_config,
        in_ci=in_ci,
        variables=variables,
    )


def build_handles(
    settings: LocalSettings, resolved: Sequence[ResolvedServer], *, environ: Mapping[str, str]
) -> list[McpServerHandle]:
    """``environ`` is the environment stdio servers may inherit from (only PATH, LANG and LC_ALL
    are passed on). The SDK logs raw provider bytes, so its logger is silenced here, at the root."""
    logging.getLogger("mcp").setLevel(logging.CRITICAL)
    runtime = StdioRuntime(inherited_env=dict(environ))
    limits = McpLimits(
        max_response_bytes=settings.mcp_max_stdio_bytes,
        max_list_tools_bytes=settings.mcp_max_list_tools_bytes,
        max_tools=settings.mcp_max_tools,
        # Each phase ends before the overall deadline does, so the client's own classification of
        # why it failed (a timeout, or a server that only ever spoke garbage) is what the summary
        # states; the overall deadline stays the backstop.
        timeouts=ClientTimeouts(
            connect_seconds=settings.mcp_deadline_seconds * 0.8,
            call_seconds=settings.mcp_deadline_seconds * 0.8,
        ),
    )
    slots = asyncio.Semaphore(settings.mcp_max_processes)
    handles: list[McpServerHandle] = []
    for server in resolved:
        spec = server.spec
        host: str | None = None
        target: StdioTarget | HttpTarget
        if spec.transport == "stdio":
            if spec.command is None:
                raise McpConfigError(f"MCP server {spec.name!r}: its entry has no command")
            target = StdioTarget(spec.command, spec.args, spec.plain_env())
        else:
            if spec.url is None:
                raise McpConfigError(f"MCP server {spec.name!r}: its entry has no url")
            url = spec.url.get_secret_value()
            target = HttpTarget(url, spec.plain_headers())
            host = url_host(spec.name, url)
        handles.append(
            McpServerHandle(
                name=spec.name,
                transport=spec.transport,
                host=host,
                tool=server.nomination.tool,
                client=_ToolClient(
                    McpToolClient(target, limits=limits, process_slots=slots, runtime=runtime),
                    tool_trusted=not server.nomination.from_file,
                ),
            )
        )
    return handles


def build_provider(
    settings: LocalSettings,
    handles: Sequence[McpServerHandle],
    *,
    diff: Callable[[], Diff | None],
    finder: SecretLineFinder,
    consult: bool,
) -> tuple[McpKnowledgeProvider, LocalMcpResolver, LocalMcpOpener]:
    provider = McpKnowledgeProvider(
        handles,
        diff=diff,
        finder=finder,
        deadline_seconds=settings.mcp_deadline_seconds,
        max_calls=settings.mcp_max_calls,
        consult=consult,
    )
    label = "MCP knowledge (" + ", ".join(h.name for h in handles) + ")"
    return provider, LocalMcpResolver(label), LocalMcpOpener(provider)


def first_item_id() -> int:
    """The id the first MCP item takes: local mode has no code items, so the bundle starts empty."""
    return ContextBundle().next_item_id()


def finding_label(finding: Finding) -> str:
    return finding.file if finding.line is None else f"{finding.file}:{finding.line}"


def mcp_outcomes(
    provider: McpKnowledgeProvider | None, findings: Sequence[Finding]
) -> list[McpServerOutcome]:
    """The run summary's per-server outcomes, with which findings each server's context backed."""
    if provider is None:
        return []
    supplied: dict[str, list[str]] = {name: [] for name in provider.records}
    for finding in findings:
        for name in provider.suppliers(finding.context_ref_ids or (), first_id=first_item_id()):
            supplied[name].append(finding_label(finding))
    return [
        McpServerOutcome(
            name=record.name,
            status=record.status,
            reason=record.reason,
            truncated_items=record.truncated_items,
            supplied_findings=supplied[record.name],
        )
        for record in provider.records.values()
    ]


def annotation(provider: McpKnowledgeProvider) -> Callable[[Finding], str | None]:
    """The line printed under a finding that cited MCP context."""

    def note(finding: Finding) -> str | None:
        names = provider.suppliers(finding.context_ref_ids or (), first_id=first_item_id())
        return f"context from MCP server {', '.join(names)}" if names else None

    return note


__all__ = [
    "McpConfigError",
    "annotation",
    "build_handles",
    "build_provider",
    "mcp_outcomes",
    "resolve_nominated",
]
