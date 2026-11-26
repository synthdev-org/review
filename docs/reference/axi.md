# AXI — Agent Experience Interface

[github.com/kunchenguid/axi](https://github.com/kunchenguid/axi)

AXI defines 10 design principles for building "agent-ergonomic" CLI tools —
command-line interfaces designed for consumption by LLM agents rather than
humans. Its core premise is that token budget is a first-class design
constraint, and that a well-designed agent-native CLI can outperform both
traditional human-oriented CLIs and structured tool protocols like MCP on
cost and reliability.

Reported benchmarks from the project:

- Browser automation: 100% task success at $0.074 average cost per task,
  vs. $0.101 for an equivalent MCP-based tool.
- GitHub operations: 100% task success at $0.050 average cost, vs. $0.148
  for GitHub's own MCP server.

## The 10 principles

1. **Token-efficient output** — use a compact serialization (AXI's
   reference tooling favors TOON over JSON) for roughly 40% fewer tokens
   per response.
2. **Minimal default schemas** — list output defaults to 3–4 fields per
   item rather than the 10+ a human-facing API might return.
3. **Content truncation with escape hatches** — truncate long text with a
   visible size indicator, and expose a `--full` flag to fetch the
   untruncated version on demand.
4. **Pre-computed aggregates** — return summaries and derived statuses
   inline so the agent doesn't need a follow-up round trip to compute
   them itself.
5. **Definitive empty states** — return an explicit "0 results" rather
   than an empty or ambiguous response the agent has to interpret.
6. **Structured errors & exit codes** — mutations are idempotent, errors
   are structured (not free text), there are no interactive prompts, and
   unrecognized flags fail loudly instead of being silently ignored.
7. **Ambient context** — prefer opt-in session/environment integrations
   first, falling back to an on-demand "skill" the agent can invoke
   explicitly.
8. **Content first** — running a command with no arguments shows live
   data, not a help screen.
9. **Contextual disclosure** — every response suggests relevant next
   steps rather than leaving the agent to guess the next command.
10. **Consistent way to get help** — each subcommand exposes concise,
    agent-consultable reference material in a uniform place.

## Structure

The reference implementation lives in a monorepo (Node.js/pnpm workspaces):

- `packages/` — the AXI SDK for JavaScript
- `bench-browser/`, `bench-github/` — benchmark harnesses used to produce
  the cost/success numbers above
- `.agents/skills/` — agent skill definitions, including the full AXI
  skill spec that expands on each principle
- `docs/` — project documentation

AXI also maintains official reference CLIs (`gh-axi`, `chrome-devtools-axi`)
and catalogs 50+ community-built AXIs across tools like Slack, Notion,
Databricks, and Kubernetes.
