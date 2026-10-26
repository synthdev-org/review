"""adapter.local.mcp_config — which MCP servers a run may consult, and how to reach them (ADR-0017).

Two separate things, kept apart on purpose:

* **Nominations** say *which* servers review may start or contact, by name only (``--mcp NAME`` or
  the committed file ``.review/mcp.json``). They never say how: a nomination file with any of
  ``command``, ``args``, ``env``, ``url``, ``headers`` or ``cwd`` anywhere in it is rejected
  (naming the key and line, never the value), because a repository file choosing a command is
  remote code execution on whoever runs review. In CI the file is read from the **base** commit,
  so a pull request cannot nominate a server.
* **Definitions** say *how* to reach a nominated server and come only from the developer's own
  MCP configuration: ``~/.claude.json`` (project scope, then user scope), the Claude Desktop file,
  and last the repository's ``.mcp.json``. In CI none of those is read: the only configuration is
  the file the workflow itself provides (``REVIEW_LOCAL_MCP_CONFIG``), which must lie outside the
  checkout, and a stdio entry is usable there only because the workflow defined it.

Hardening beyond the ADR text: a server defined only by the repository's own ``.mcp.json`` and
nominated only by the repository's own ``.review/mcp.json`` would let a cloned repository pick a
command to run on the developer's machine, so such a server needs an explicit ``--mcp``.

Secrets (env values, headers, URLs) are held in ``SecretStr`` fields excluded from ``repr``; only
server names, key names and line numbers appear in an error message.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from adapter.local.exceptions import MemoryFileError
from adapter.local.memory_file import (
    MEMORY_FILE_MAX_BYTES,
    read_bounded,
    read_from_commit,
    read_working_tree,
    safe_relative,
)
from adapter.product_knowledge.mcp_tools import SEARCH_WORDS

DEFAULT_NOMINATION_FILE = ".review/mcp.json"
CONFIG_FILE_MAX_BYTES = MEMORY_FILE_MAX_BYTES  # 256 KiB, for every file but the next
# ~/.claude.json holds the developer's whole history next to the server maps, so it is far larger
# than a server list; it is the developer's own file and is dropped after the maps are read.
USER_CONFIG_MAX_BYTES = 8 * 1024 * 1024
MAX_NOMINATED_SERVERS = 3

FORBIDDEN_KEYS = ("command", "args", "env", "url", "headers", "cwd")
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TOOL = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
# ``${VAR}`` and ``${VAR:-default}``, the expansion Claude Code applies to a server's entry.
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

Source = Literal["explicit", "claude_project", "claude_user", "desktop", "repo"]


class McpConfigError(Exception):
    """The nominations or the MCP configuration cannot be used (exit status 2). The message names
    servers, keys, paths and line numbers; it never holds a value from the configuration."""


@dataclass(frozen=True)
class Nomination:
    name: str
    tool: str | None = None
    from_file: bool = False  # True: named by a repository file, not by the developer's option


class McpServerSpec(BaseModel):
    """How to reach one server. Every value that can be a credential is a ``SecretStr`` kept out
    of ``repr``."""

    model_config = ConfigDict(frozen=True)

    name: str
    transport: Literal["stdio", "http"]
    source: Source
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    url: SecretStr | None = Field(default=None, repr=False)
    headers: dict[str, SecretStr] = Field(default_factory=dict, repr=False)

    def plain_env(self) -> dict[str, str]:
        return {k: v.get_secret_value() for k, v in self.env.items()}

    def plain_headers(self) -> dict[str, str]:
        return {k: v.get_secret_value() for k, v in self.headers.items()}


@dataclass(frozen=True)
class ResolvedServer:
    nomination: Nomination
    spec: McpServerSpec


# --- nominations --------------------------------------------------------------------------------


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    tool: str | None = None


class _Servers(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    servers: list[str | _Entry] = Field(max_length=MAX_NOMINATED_SERVERS)


class _NominationFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mcp: _Servers


def parse_option(value: str) -> Nomination:
    """``NAME`` or ``NAME:TOOL`` from ``--mcp``."""
    name, _, tool = value.partition(":")
    if not _NAME.fullmatch(name):
        raise McpConfigError(
            "--mcp takes a server name of letters, digits, '.', '_' and '-' (at most 64)"
        )
    if tool and not _TOOL.fullmatch(tool):
        raise McpConfigError(f"--mcp {name}: the tool name is not valid")
    return Nomination(name=name, tool=tool or None)


def _forbidden_key(node: Any) -> str | None:
    """The first forbidden key anywhere in the document. Iterative: a hostile file nested
    thousands deep must be rejected, not crash the recursion limit."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key in current:
                if key in FORBIDDEN_KEYS:
                    return str(key)
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))
    return None


def _line_of_key(text: str, key: str) -> int | None:
    pattern = re.compile(rf'"{re.escape(key)}"\s*:')
    for number, line in enumerate(text.split("\n"), start=1):
        if pattern.search(line):
            return number
    return None


def parse_nomination_file(data: bytes, *, path: str) -> tuple[Nomination, ...]:
    """The names a repository file nominates. Raises ``McpConfigError`` on anything but a names-only
    file; the message never quotes the file."""
    if len(data) > CONFIG_FILE_MAX_BYTES:
        raise McpConfigError(f"{path}: larger than the {CONFIG_FILE_MAX_BYTES}-byte limit")
    try:
        text = data.decode("utf-8")
        document = json.loads(text)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise McpConfigError(f"{path}: not valid JSON") from exc
    forbidden = _forbidden_key(document)
    if forbidden is not None:
        line = _line_of_key(text, forbidden)
        where = path if line is None else f"{path}:{line}"
        raise McpConfigError(
            f"{where}: the key '{forbidden}' is not allowed; nominations are names only "
            "(how to reach a server comes from your own MCP configuration)"
        )
    try:
        parsed = _NominationFile.model_validate(document)
    except ValidationError as exc:
        # Field locations only; pydantic's echo of the offending input is not used.
        problems = "; ".join(".".join(str(p) for p in e["loc"]) or "the file" for e in exc.errors())
        raise McpConfigError(
            f'{path}: expected {{"mcp": {{"servers": ["name", ...]}}}} with at most '
            f"{MAX_NOMINATED_SERVERS} servers (problem at: {problems})"
        ) from exc
    nominations: list[Nomination] = []
    for entry in parsed.mcp.servers:
        name, tool = (entry, None) if isinstance(entry, str) else (entry.name, entry.tool)
        if not _NAME.fullmatch(name) or (tool is not None and not _TOOL.fullmatch(tool)):
            raise McpConfigError(f"{path}: a server or tool name is not valid")
        # A repository file must not pick an arbitrary tool (it would be called with diff-derived
        # text): only read-only search-style names are honoured; others need the --mcp option.
        if tool is not None and not any(w in tool.lower() for w in SEARCH_WORDS):
            raise McpConfigError(
                f"{path}: a tool named in the file must contain 'search', 'query' or 'lookup'; "
                "name other tools with --mcp"
            )
        nominations.append(Nomination(name=name, tool=tool, from_file=True))
    return tuple(nominations)


async def load_nomination_file(
    repo_dir: Path, path: str, *, base_commit: str | None, environ: Mapping[str, str]
) -> tuple[Nomination, ...]:
    """The repository's nomination file, or ``()``. ``base_commit`` set (CI): read from that commit
    and never from the working tree, which is the pull-request head."""
    try:
        relative = safe_relative(path)
        data = (
            await asyncio.to_thread(read_working_tree, repo_dir, relative, str(relative))
            if base_commit is None
            else await read_from_commit(
                repo_dir, base_commit, relative, str(relative), environ=environ
            )
        )
    except MemoryFileError as exc:
        raise McpConfigError(f"{path}: {exc.reason}") from exc
    return () if data is None else parse_nomination_file(data, path=str(relative))


def merge_nominations(
    from_option: tuple[Nomination, ...], from_file: tuple[Nomination, ...]
) -> tuple[Nomination, ...]:
    """The option wins over the file for the same name; the total is capped."""
    merged: dict[str, Nomination] = {n.name: n for n in from_file}
    merged.update({n.name: n for n in from_option})
    if len(merged) > MAX_NOMINATED_SERVERS:
        raise McpConfigError(f"at most {MAX_NOMINATED_SERVERS} MCP servers can be nominated")
    return tuple(merged.values())


# --- definitions --------------------------------------------------------------------------------


def _read_json_file(path: Path, *, limit: int, follow_symlinks: bool) -> Any | None:
    """The JSON document at ``path``, or ``None`` if there is no such file."""
    try:
        data = read_bounded(path, path.name, limit=limit, follow_symlinks=follow_symlinks)
    except MemoryFileError as exc:
        raise McpConfigError(f"{path.name}: {exc.reason}") from exc
    if data is None:
        return None
    try:
        return json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise McpConfigError(f"{path.name}: could not be read as JSON") from exc


def _server_map(document: Any, *keys: str) -> dict[str, Any]:
    node = document
    for key in (*keys, "mcpServers"):
        if not isinstance(node, dict) or key not in node:
            return {}
        node = node[key]
    return node if isinstance(node, dict) else {}


def _expand(value: str, name: str, variables: Mapping[str, str] | None) -> str:
    """``value`` with ``${VAR}``/``${VAR:-default}`` replaced from ``variables``; ``None`` leaves
    it verbatim. An unset variable without a default is an error naming the variable, never a
    value."""
    if variables is None:
        return value

    def replace(match: re.Match[str]) -> str:
        found = variables.get(match.group(1), match.group(2))
        if found is None:
            raise McpConfigError(
                f"MCP server {name!r}: ${{{match.group(1)}}} is used in its configuration but is "
                "not set"
            )
        return found

    return _VARIABLE.sub(replace, value)


def _string_map(
    raw: Any, name: str, what: str, variables: Mapping[str, str] | None
) -> dict[str, SecretStr]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise McpConfigError(f"MCP server {name!r}: its {what} are not a map of strings")
    return {k: SecretStr(_expand(v, name, variables)) for k, v in raw.items()}


def url_host(name: str, url: str) -> str | None:
    """The host of a configured URL (never userinfo, port, path or query). A URL that cannot be
    split is a configuration error naming the server, never a traceback or the URL."""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 -- a bad port raises ValueError only when read
        return parts.hostname
    except ValueError:
        raise McpConfigError(f"MCP server {name!r}: its url is not a valid URL") from None


def parse_entry(
    name: str, raw: Any, source: Source, variables: Mapping[str, str] | None = None
) -> McpServerSpec:
    """One ``mcpServers`` entry as a spec; ``McpConfigError`` (no values in it) if unusable.
    ``variables`` is what ``${VAR}`` expands from; the repository's own file never expands, or a
    pull request could name a variable of the developer's and send it to its own server."""
    if source == "repo":
        variables = None
    if not isinstance(raw, dict):
        raise McpConfigError(f"MCP server {name!r}: its configuration entry is not an object")
    kind = raw.get("type")
    command, url = raw.get("command"), raw.get("url")
    if kind in ("sse", "ws", "websocket"):
        raise McpConfigError(f"MCP server {name!r}: the {kind} transport is not supported")
    if isinstance(command, str) and command and kind in (None, "stdio"):
        args = raw.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise McpConfigError(f"MCP server {name!r}: its args are not a list of strings")
        return McpServerSpec(
            name=name,
            transport="stdio",
            source=source,
            command=_expand(command, name, variables),
            args=tuple(_expand(a, name, variables) for a in args),
            env=_string_map(raw.get("env"), name, "env values", variables),
        )
    if isinstance(url, str) and url and kind in (None, "http", "streamable-http"):
        url = _expand(url, name, variables)
        url_host(name, url)  # reject an unusable URL now, before any run starts
        return McpServerSpec(
            name=name,
            transport="http",
            source=source,
            url=SecretStr(url),
            headers=_string_map(raw.get("headers"), name, "headers", variables),
        )
    raise McpConfigError(f"MCP server {name!r}: its entry has neither a command nor a url")


def default_desktop_config(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def _outside(path: Path, repo_dir: Path) -> bool:
    resolved, root = path.resolve(), repo_dir.resolve()
    return resolved != root and root not in resolved.parents


def _candidate_maps(
    *,
    repo_dir: Path,
    home: Path,
    explicit_config: Path | None,
    in_ci: bool,
    desktop_config: Path | None,
) -> list[tuple[Source, dict[str, Any]]]:
    """The server maps to search, most trusted first."""
    if explicit_config is not None:
        if in_ci and not _outside(explicit_config, repo_dir):
            raise McpConfigError(
                "REVIEW_LOCAL_MCP_CONFIG points inside the checkout, which a pull request "
                "controls; provide a file outside it"
            )
        document = _read_json_file(
            explicit_config, limit=CONFIG_FILE_MAX_BYTES, follow_symlinks=True
        )
        if document is None:
            raise McpConfigError("REVIEW_LOCAL_MCP_CONFIG names a file that does not exist")
        return [("explicit", _server_map(document))]
    if in_ci:
        raise McpConfigError(
            "CI was detected and no MCP configuration was provided: set "
            "REVIEW_LOCAL_MCP_CONFIG to a file outside the checkout (your own configuration and "
            "the repository's .mcp.json are not read in CI)"
        )
    maps: list[tuple[Source, dict[str, Any]]] = []
    claude = _read_json_file(
        home / ".claude.json", limit=USER_CONFIG_MAX_BYTES, follow_symlinks=True
    )
    if claude is not None:
        maps.append(("claude_project", _server_map(claude, "projects", str(repo_dir.resolve()))))
        maps.append(("claude_user", _server_map(claude)))
    desktop = _read_json_file(
        desktop_config or default_desktop_config(home),
        limit=CONFIG_FILE_MAX_BYTES,
        follow_symlinks=True,
    )
    if desktop is not None:
        maps.append(("desktop", _server_map(desktop)))
    # Last: the repository's file must never shadow the developer's own server of the same name.
    repo = _read_json_file(
        repo_dir / ".mcp.json", limit=CONFIG_FILE_MAX_BYTES, follow_symlinks=False
    )
    if repo is not None:
        maps.append(("repo", _server_map(repo)))
    return maps


def resolve_servers(
    nominations: tuple[Nomination, ...],
    *,
    repo_dir: Path,
    home: Path,
    explicit_config: Path | None,
    in_ci: bool,
    desktop_config: Path | None = None,
    variables: Mapping[str, str] | None = None,
) -> list[ResolvedServer]:
    """The definition of every nominated server. Nothing is read when nothing is nominated.
    ``variables`` (the composition root's copy of the developer's environment) is what ``${VAR}``
    in a developer-owned entry expands from.
    Raises ``McpConfigError`` for a name that is in no configuration, an unusable entry, or a
    server defined and nominated only by repository files."""
    if not nominations:
        return []
    maps = _candidate_maps(
        repo_dir=repo_dir,
        home=home,
        explicit_config=explicit_config,
        in_ci=in_ci,
        desktop_config=desktop_config,
    )
    resolved: list[ResolvedServer] = []
    for nomination in nominations:
        found = next(
            (
                (source, entries[nomination.name])
                for source, entries in maps
                if nomination.name in entries
            ),
            None,
        )
        if found is None:
            raise McpConfigError(
                f"MCP server {nomination.name!r} was nominated but is not in your MCP configuration"
            )
        spec = parse_entry(nomination.name, found[1], found[0], variables)
        if spec.source == "repo" and nomination.from_file:
            raise McpConfigError(
                f"MCP server {nomination.name!r} is defined by the repository's .mcp.json and "
                "nominated by the repository's nomination file, so the repository would choose "
                "what runs on this machine; nominate it yourself with --mcp"
            )
        resolved.append(ResolvedServer(nomination=nomination, spec=spec))
    return resolved
