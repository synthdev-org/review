# review

**Self-hosted, enterprise AI code review platform.** A genuine competitor to
hosted tools like CodeRabbit, Qodo, and Greptile — not a diff-into-an-LLM
wrapper — built to be deployed inside *your own* infrastructure so your code
and review history never leave your control.

[![License: review Community License](https://img.shields.io/badge/license-review%20Community%20License-blue)](LICENSE)

---

## Why this exists

Most AI code review tools are SaaS: your diffs, your source, and your team's
review history all flow through someone else's servers. `review` takes the
opposite approach — it ships as container images *you* deploy, on Kubernetes
in production or Docker Compose locally, talking only to the GitHub org and
LLM endpoint(s) you configure. We never see your code.

The architecture isn't a guess at what a good review bot looks like — it's
derived from research into how the leading commercial tools actually achieve
thorough review (see [`docs/research/research-current-art.md`](docs/research/research-current-art.md))
and built to match or beat them on the techniques that matter:

- **Real indexing/RAG, not a bare diff.** AST-aware chunking, embeddings, and
  a call/import graph, kept incrementally fresh on every push, so review
  comments are grounded in how your codebase actually fits together — not
  just the lines that changed.
- **Static analysis first.** Linting, secret scanning, and static analyzers
  run before any LLM call, for free, and act as the deterministic trust
  anchor the LLM's own findings get checked against.
- **Structurally separate verification, never self-critique.** The most
  consistently reproduced finding across vendors is that asking a model to
  re-rate its own output is close to random. Verification always runs as an
  independent call against a fixed rubric.
- **Continual, human-validated learning.** The platform learns from the
  reviews it performs — dismissed findings, accepted fixes, developer
  reactions — but every learning is surfaced for a human to approve or
  remove before it reshapes future reviews. Never a silent black box.
- **Bring-your-own-LLM, any endpoint.** The platform never proxies inference
  or holds provider credentials of its own. Point it at a hosted API
  (Anthropic, OpenAI, Azure OpenAI, Bedrock), a self-hosted model server
  (vLLM, TGI), or a local model — anything speaking an OpenAI- or
  Anthropic-compatible wire format.
- **Optional product-knowledge augmentation, never a dependency.** A
  separate self-hosted product ([knowhow](../knowhow)) can be connected over
  MCP to pull in non-code context (Confluence, Slack, tickets). Its absence
  never degrades review's core capability.

See [`AGENTS.md`](AGENTS.md) for the full project overview,
[`docs/reference/c4-architecture.md`](docs/reference/c4-architecture.md) for
the system architecture (with diagrams), and [`docs/roadmap.md`](docs/roadmap.md)
for how the platform is being delivered in phases.

## How it works

```
GitHub PR event ──▶ Webhook API ──▶ Job Queue (Postgres) ──▶ Worker
                                                                 │
                    ┌────────────────────────────────────────────┤
                    │ Indexing job                Review job      │
                    │ AST chunking,                Pre-filter      │
                    │ embeddings,                  Static analysis │
                    │ call/import graph            Context Builder │
                    │ → Postgres/pgvector          LLM Gateway      │
                    │                              Verify + merge   │
                    └────────────────────────────────────────────┤
                                                                 │
                                                                 ▼
                                              GitHub Adapter posts inline
                                              PR comments + check-run status
```

Two deployable components — a **Webhook API** (FastAPI) and a **Worker** —
share one Postgres/pgvector database that holds the code index, findings
history, team Memory, and the job queue itself (no separate broker). Every
LLM call goes through a **LLM Gateway** built on
[LiteLLM](https://github.com/BerriAI/litellm), routed per org/team/pipeline
stage to whichever endpoint the customer has configured.

For the full breakdown — system context, container diagram, and the LLM
Gateway's component view — read
[`docs/reference/c4-architecture.md`](docs/reference/c4-architecture.md).

## Project status

`review` is under active, phased development (see
[`docs/roadmap.md`](docs/roadmap.md) for the full sequencing). As of this
writing, the core review pipeline is implemented end-to-end: webhook
ingestion, incremental AST-aware indexing, static analysis, context
building, LLM-based review generation, structurally-separate verification,
and OpenTelemetry-based observability across the pipeline. The Helm chart
(`charts/review/`) is **pre-1.0 / unstable** — don't build automation
against its `values.yaml` shape yet. Memory (continual learning), the admin
UI, and multi-org tenancy are still ahead on the roadmap.

## Getting started

The whole stack (Webhook API + Worker + Postgres/pgvector) runs via Docker
Compose, using the same container images Kubernetes runs in production.

1. Copy `.env.example` to `.env` and replace every placeholder with a real
   value — `.env` is gitignored, so this never risks committing a secret.
2. Optionally start [LM Studio](https://lmstudio.ai/) locally with a model
   loaded and its OpenAI-compatible server running on port `1234` — the
   Worker's LLM Gateway points at `host.docker.internal:1234` by default,
   so no cloud LLM credentials are required to try the platform locally.
3. Run:

   ```sh
   docker compose up
   ```

   This blocks until Postgres is healthy, migrations (including the
   pgvector-presence check) have applied, and the Webhook API's `/healthz`
   endpoint returns 200.

### Production deployment

Production targets Kubernetes via the Helm chart in
[`charts/review/`](charts/review/). See
[`docs/reference/operations.md`](docs/reference/operations.md) for
operator-facing guidance (restart-looping after an upgrade, rate-limiting
defaults, running migrations before a Helm upgrade). As noted above, the
chart is pre-1.0 — expect its `values.yaml` shape to change before the
roadmap's "Production hardening" phase.

## Architecture at a glance

| Component | Role |
|---|---|
| `apps/webhook_api` | FastAPI service — verifies GitHub webhook signatures, resolves the installation/org/repo, enqueues review/reindex jobs, serves the GitHub App callback. |
| `apps/worker` | Horizontally-scalable worker handling both indexing and review job types. |
| `indexing/` | AST-aware chunking, embeddings, call/import graph extraction. |
| `review/` | The review pipeline: pre-filter, static analysis, context building, findings merge/verify. |
| `llmgateway/` | LiteLLM-based routing across any OpenAI- or Anthropic-compatible endpoint, configured per org/team/pipeline stage. |
| `findings/`, `memory/`, `tenancy/`, `ingestion/` | Domain packages for findings modeling, team Memory, org/repo/team scoping, and inbound webhook ingestion. |
| `adapter/` | Adapters for GitHub, static analyzers, embeddings, storage, and telemetry — the only code that talks to the outside world. |
| `charts/review/` | Helm chart for production Kubernetes deployment. |
| `observability/` | Grafana/OpenTelemetry configuration for the local stack. |

For the package-boundary rules that shape this layout (domain vs. adapter,
Protocol-defined dependencies, config handling), see
[`docs/rules/architecture.md`](docs/rules/architecture.md).

## Testing

The test suite follows a strict pyramid — unit tests for domain packages
(no I/O), integration tests for adapters (real Postgres via
`testcontainers-python`, recorded LLM/GitHub cassettes), and a thin layer of
end-to-end tests driving the Docker Compose stack over HTTP. Full rules,
including how PRD user stories map to required integration tests, live in
[`docs/rules/testing.md`](docs/rules/testing.md).

```sh
pytest              # unit tests (default, fast)
pytest -m integration   # integration tests
```

## Documentation

- [`AGENTS.md`](AGENTS.md) — project overview and mandatory reading for
  contributors.
- [`docs/reference/c4-architecture.md`](docs/reference/c4-architecture.md) —
  system architecture (C4 diagrams).
- [`docs/roadmap.md`](docs/roadmap.md) — delivery phases and sequencing.
- [`docs/adr/`](docs/adr/) — architecture decision records.
- [`docs/rules/architecture.md`](docs/rules/architecture.md) and
  [`docs/rules/testing.md`](docs/rules/testing.md) — mandatory engineering
  rules for this repo.
- [`docs/research/research-current-art.md`](docs/research/research-current-art.md) —
  the survey of existing tools this architecture is derived from.

## License

Licensed under the **review Community License** (Elastic License
2.0-based) — free below a company-size threshold, paid above it. See
[`LICENSE`](LICENSE) for the full terms.
