# ADR: Local mode MCP knowledge — nominated servers, names-only nominations, bounded tool use

## Status

PROPOSED (drafted 2026-10-25 after the Phase 6 spike; becomes ACCEPTED when the owner
signs off on the security posture and on the go/no-go in Decision 11). Phase 7 (US-L9 to
US-L11) has been **built** on this proposal (Decision 11 allows building it), but it is not
accepted, and owner acceptance is tracked outside the code. Open question **Q2 is UNPROVEN** (see
"Spike evidence"); the live run has not been done.

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

Local mode ([ADR-0016](0016-local-mode-composition-root.md)) has no code index; its
repository and product context comes from MCP servers the developer already runs
(spec D3). This ADR decides how review finds those servers, which ones it may start or
contact, how it picks a tool and a query from a diff, and which caps and scrubs apply,
so that "use my MCP servers" cannot become "a pull request runs commands on my CI
runner" or "my tokens leave the machine".

## Context

- The existing client (`adapter/product_knowledge/client.py`, ADR-0013) is
  Streamable-HTTP only, egress-guarded, and bound to a four-tool contract. Stdio and
  generic tool use are new.
- MCP configuration is client-owned and carries secrets (env, headers, URLs). Review
  must read it without ever displaying or forwarding it.
- A stdio server entry is a command line. Anything that lets a repository file choose
  a command is remote code execution on whoever runs review (CI included).
- Tool results are attacker-influenced text (a server may index PR content).

## Spike evidence

Throwaway code (kept in `/tmp`, not in the repo) used the official `mcp` 2.x SDK against
two stub servers built on the SDK (`MCPServer`): stdio and Streamable HTTP on loopback.
Observed:

1. **Config discovery.** Structure only (names, transport, command basename) was read
   from: a project `.mcp.json` (`{"mcpServers": {name: {command,args,env} | {type:"http",
   url,headers}}}`; the type key is absent for stdio) and the user-level
   `~/.claude.json`, which holds a user-level `mcpServers` map at top level (10 servers on
   the owner's machine) and per-project maps under `projects.<path>.mcpServers` (9 distinct
   servers: 7 HTTP and 2 stdio, 3 of the HTTP ones with headers, i.e. credentials).
   `~/.claude/settings.json` holds none.
   Other clients (Cursor, VS Code, Claude Desktop) use
   the same `mcpServers` shape in different files but were not exercised.
2. **Real servers.** None was contacted. The project-scope HTTP servers are hosted services
   that need a credential or have write tools; the stdio ones are run via
   `python`/`npx` and were skipped (host Python is forbidden here; `npx` fetches code).
   So no real-server evidence exists. This is a limit of the spike, not a result.
3. **SDK behaviour.** `Client` lists tools and calls them over both transports.
   `stdio_client` with no `env` gave the child only `HOME`, `LC_CTYPE`, `PATH`: a planted
   `GITHUB_TOKEN` and model-key variable were absent. A 2,000,000-character tool result
   was returned whole (the SDK applies no size cap). A hanging tool is only stopped by our
   own `asyncio.timeout`. `tool.inputSchema` is available from `list_tools`, so declared
   arguments can be honoured as server mode does.
4. **Q2 (does MCP context improve a review?): UNPROVEN.** No model endpoint was used, so
   there is no relevance evidence. A canned stub answering "charge must be idempotent"
   to a query containing "charge" shows only that the plumbing works.

**Manual live-run procedure to settle Q2** (owner, before Phase 7 ships to users): pick a
real nominated server that holds a fact absent from the repo (an ADR, runbook or policy
page); pick a fixture PR that violates that fact and that a no-context review does not
flag. Run the review twice against the same real model, with and without the nomination,
five times each, `--dry-run` first to read the query. Record: the query sent, the tool
chosen, whether the finding citing the fact appears, and false positives added. Go if
the fact appears in at least 3 of 5 runs with context and 0 of 5 without, and no new
false positives; otherwise no-go (Decision 11).

## Decision

1. **Config sources read.** The developer's user-level client configuration (the
   `~/.claude.json` user and per-project scopes, the platform Claude Desktop file) and,
   locally only, the project `.mcp.json` at the repository root. A setting
   `REVIEW_LOCAL_MCP_CONFIG` can point at one explicit file. **In CI the repo-local
   `.mcp.json` is never read**, and user-level files do not exist on a runner; the only
   config in CI is a file the workflow itself provides by that setting, from a path
   outside the checkout. Files are read once into memory, size-capped (256 KiB), regular
   files only, and parsed with `json` into a pydantic model that keeps only the fields in
   Decision 4. Everything else, including env and header values, is dropped at the
   boundary and never held in a loggable object.
2. **Nothing runs unless nominated.** Review starts or contacts a server only if its
   name is nominated. An unknown nominated name fails the run with exit 2 before any
   model call; an un-nominated server in the config is never touched.
3. **Nominations are names only.** Schema: `{"mcp": {"servers": ["docs", "kb"]}}` or
   `--mcp docs --mcp kb`. A name matches `^[A-Za-z0-9_.-]{1,64}$`. The nomination
   file model forbids extra keys, and **any** of `command`, `args`, `env`, `url`,
   `headers`, `cwd` anywhere in it is rejected (exit 2, naming the key and line, never
   the value). The same rejection applies to any repo file review reads.
4. **Where nominations live.** The command option `--mcp NAME` and a committed
   names-only file `.review/mcp.json`. Locally the file is read from the working tree.
   **In CI it is read from the base commit** (same `git ls-tree`/`cat-file` path as the
   memory file in ADR-0016 Decision 5d) so a pull request cannot add a server or a
   name. A `--mcp` option in the workflow file is trusted (the workflow is the owner's).
5. **CI refuses stdio unless the workflow defines it.** In CI a stdio entry is usable
   only if it comes from the workflow-provided config file (Decision 1) *and* is
   nominated. Otherwise exit 2 naming the server. HTTP entries are usable in CI by name
   from that same file. The runbook states that giving CI a stdio command is the
   workflow author's decision and runs on their runner.
6. **Scrubbed child environment.** A stdio child gets an allow-list: `PATH`, `HOME`
   (a fresh empty temp directory), `LANG`, `LC_ALL`, plus only the variables the
   developer's config lists under that server's `env` (those values live in memory and
   are never printed). `GITHUB_TOKEN`, `GH_TOKEN`, model keys and everything else
   inherited are absent; the spike showed the SDK default already behaves this way and
   the implementation passes `env` explicitly so it does not depend on that default. The
   working directory is a fresh empty temp directory.
7. **Caps (all defaults, all settings).** Per run: at most 3 nominated servers, 1 tool
   call per server (no retries), 3 calls
   total; `list_tools` response at most 64 KiB and 100 tools (extra are ignored); stdio
   bytes read per server at most 256 KiB, enforced by a bounded reader on the stream, not
   after the fact; each result truncated to 4,000 characters (server-mode content limit)
   and a snippet to 1,000; at most 3 concurrent child processes; per-server deadline
   10 s and all MCP together 10 s (spec), enforced with `asyncio.timeout` around connect,
   list and call. On any breach, timeout, cancellation or exit, the child is killed and
   reaped (SIGTERM, short grace, SIGKILL, `wait`) and the stage is reported as failed for
   that server; the review continues without it (US-L10). No retry, no circuit breaker.
8. **Tool and query choice, deliberately simple.** The nomination may carry an optional
   tool name per server (`{"servers": [{"name": "docs", "tool": "search_docs"}]}`; the
   `--mcp docs:search_docs` form). Without one, review picks the single *eligible* listed tool
   whose name contains `search`, `query` or `lookup`; if none or several, the server is
   skipped and reported "no usable tool", never guessed (several is not resolved by name
   order). **A tool is eligible only if the server declares `readOnlyHint: true` (and not
   `destructiveHint` or `openWorldHint` true), or the developer named it themselves on the
   command line (`--mcp NAME:TOOL`); a tool named only by the repository file must still
   be declared read-only.** The one argument sent goes only into a parameter named
   `query`, `q`, `search`, `text`, `keywords` or `prompt` (that preference); there is no
   fallback to any other parameter, and a tool whose schema requires any other parameter is
   skipped as "no usable tool". Diff-derived text is therefore never placed in a parameter a
   server may execute (`sql`, `command`, `expression`, a path or URL).
   The query is built, never model-written: `"<changed file paths (basenames
   and parent dir)> <up to 10 added/changed symbol names>"`, capped at 500 characters,
   **built from the redacted diff** (ADR-0016 trust gate: US-L15) so a secret never
   reaches a server. Tool descriptions are ignored for behaviour, as in ADR-0013; annotations
   are read only for the eligibility rule above. A smarter model-driven tool choice is out of
   scope until Q2 is proven; the trigger to revisit is a go on Decision 11.
9. **Egress policy.** HTTP servers go through the existing guard
   (`adapter/product_knowledge/transport.py`): redirects are never followed, the
   resolved address is pinned, no environment proxy or netrc. **Loopback is allowed for
   a nominated server whose configured host is literally `localhost`, `127.0.0.1` or
   `::1`, in local runs only**, because developers run servers locally. In CI loopback
   stays denied unless the workflow-provided config names it. Link-local, the cloud
   metadata addresses (169.254.169.254 and the other listed ones), unspecified and
   multicast stay blocked everywhere with no setting. Private ranges follow the existing
   default. This is a new policy value, not an edit to the server-mode guard defaults.
10. **Tokens never cross a redirect; credentials never leave their host.** A header
    from the developer's config is attached only to requests to that entry's own
    scheme+host+port (the ADR-0013 host-binding rule). Because redirects are refused
    outright there is no follow path to leak through. Header and env values are masked
    (`mask_url`-style) in every message; `--dry-run` shows server name, transport, tool,
    host (no userinfo or query) and the exact query text, never headers or env.
11. **Go/no-go.** Ship Phase 7 behind the explicit nomination only (no default
    nominations, off unless named). Phase 7 may be *built* now, but its README/runbook
    marks relevance as **unproven** until the manual procedure above is run and passes.
    No-go criterion: the manual run fails the Q2 bar, in which case US-L9 to US-L11 are
    withdrawn rather than shipped as a placebo.
12. **D5 departure, local mode only.** Spec D5 lets a local MCP query carry code-derived
    terms (paths, symbol names) where server mode sends only issue keys
    (`spec-product-knowledge.md`, decision 5). It applies only to `apps/local_cli`, only to
    nominated servers, always after redaction, and is visible in `--dry-run` before
    anything is sent. The server path is unchanged; the product-knowledge spec gets a
    note, not a change. Still an assumption pending the owner's validation.
13. **Untrusted output.** Results are wrapped with `INERT_REFERENCE_DATA_FRAMING`,
    truncated per Decision 7 with the truncation recorded in the run summary, never
    parsed for instructions, and after a result arrives no further tool call or command
    is issued from its content (US-L11). The summary names each server as used / empty /
    timed out / errored.

## Alternatives considered

- **Nominations carrying `command`/`args`.** Rejected: a repo file would choose commands
  (RCE on the runner). Names only, resolved from the developer's own config.
- **Reading `.mcp.json` in CI.** Rejected: it is pull-request-controlled.
- **Model-driven tool selection and query writing.** Deferred: higher relevance
  potential, but unbounded calls, a second prompt-injection surface, and no evidence
  yet that even the simple version helps.
- **Inheriting the full environment for stdio children.** Rejected: leaks
  `GITHUB_TOKEN` and model keys to arbitrary commands.
- **Reusing the product-knowledge four-tool contract.** Rejected for local mode: the
  developer's servers do not implement it (that is the point of D3).
- **Building a code index instead.** Out of scope (spec D3).

## Consequences

- Phase 7 adds a stdio opener and a generic-tool path under `adapter/` (the subprocess
  rule), reusing the existing session and timeouts; it changes no server-mode file.
- Loopback relaxation needs a new egress-policy option, additive with the current
  behaviour as default, covered by the US-P1 suite.
- Config formats of clients other than Claude Code are not read in v1; adding one is a
  parser, not a policy change.
- Cost: a nominated server adds up to 10 s to a run and a few hundred tokens of context;
  both are visible in the summary and `--dry-run`.
- Q2 stays open. If the manual run is not done, Phase 7 may ship but cannot claim value.

## Implementation notes (Phase 7, where the build departs from or fills in the text above)

Recorded here so the owner can accept or reverse each one; none changes a decision's intent.

- **Decision 1.** `~/.claude.json` is read with an 8 MiB cap, not 256 KiB: it holds the developer's
  whole history beside the server maps, and is dropped after the maps are read. Every other file
  keeps 256 KiB. Order searched: this project's scope, user scope, the Claude Desktop file, and
  last the repository's `.mcp.json`, so the repository can never shadow a server of the same name
  in the developer's own configuration.
- **Decision 4, hardening.** A server defined only by the repository's `.mcp.json` and nominated
  only by the repository's `.review/mcp.json` is refused (exit 2): both halves would be the
  repository's, so a cloned repository would choose what runs. The developer naming it with `--mcp`
  is allowed. In CI the nomination file is read from the base commit whenever CI is detected, even
  without a recognised pull-request context.
- **Decision 7.** Reuse is partial: the SDK `Client`, `ClientTimeouts`, `GuardedTransport` and the
  failure classifiers are reused; `NegotiationCache` is not, because a nominated server speaks no
  contract and nothing is negotiated. Per-phase client timeouts are 80% of the total deadline so the
  client's own classification of a failure (timeout, or garbage) wins the race against the overall
  deadline, which stays the backstop that records `timed_out`.
- **Decision 8.** The original text said both "first by name order" and "several tie, skipped";
  the build skips when none or several eligible search-like tools exist (never guessing), and the
  Decision text above now says so. The read-only eligibility rule and the query-named-parameter
  rule were added in Phase 7 hardening (a review found that a name substring alone, with a
  fallback to any required string parameter, let pull-request-controlled text drive a tool that
  executes its argument). Symbols come from added,
  removed and nearby context lines and hunk headers, because the hunk header alone is empty for a
  change near the top of a file. Without a nominated tool name the tool list is read (a stdio
  server is started); `--dry-run` therefore shows "the tool is chosen from the server's tool list
  at run time" and starts nothing.
- **Decision 9.** Loopback is allowed by a `GuardedTransport` subclass in
  `adapter/product_knowledge/mcp_tools.py`, not a new `EgressPolicy` field, so no server-mode file
  is edited. The relaxation applies only to a host that is literally `localhost`, `127.0.0.1` or
  `::1`, and only to loopback answers; every other address is classified as before.
- **Decision 10.** Configured headers are attached by the same subclass after the base class's
  checks (target is the bound origin, allow-list, address classes) and are refused over cleartext
  http to a public address.
- **Decision 12 (D5).** The shared exposure gate (`should_query`) is satisfied with
  `allow_public_repos=True` for these providers: the gate protects tenant-configured providers from
  receiving a public repository's or a fork's content, whereas here the developer or the workflow
  author chose the server and `--dry-run` shows the query. The seed's pull-request title is a fixed
  marker so the shared context builder's provider branch runs; the query is built from the diff.
- **Unverified.** The stdio opener is POSIX only (process groups). No real third-party server was
  contacted (the spike's limit stands); the evidence is real servers built on the SDK. Value test
  (b), the live run in `apps/local_cli/test_mcp_live.py`, has not been run.

## Architecture-rule exception (Phase 7 hardening)

`docs/rules/architecture.md` says adapter code reads no files. `adapter/local/mcp_config.py` does:
the developer's `~/.claude.json`, the Claude Desktop file, the repository's `.mcp.json` and the
workflow's `REVIEW_LOCAL_MCP_CONFIG` file, plus the nomination file (from the working tree or a git
blob via `adapter/local/memory_file.py`'s readers, which are public names now). Moving the reads to
`apps/local_cli` was judged disproportionate: the CI trust rules (which file is read, from where,
whether a symlink or an in-checkout path is refused) are one security unit with their tests, and
splitting the reads from the code that decides what may be read would scatter it, for no second
consumer. The repository's existing `adapter/local/memory_file.py` reads files the same way. Everything
else the rule names is done: the inherited environment, the SDK logger setting and the teardown
registry are owned by the composition root (`apps/local_cli/mcp.py`) and passed into
`adapter/product_knowledge/mcp_tools.py` as a `StdioRuntime`; the adapter reads no `os.environ` and
keeps no module-level state. Accepting this exception is part of accepting this ADR.
