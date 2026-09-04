# Documentation Index

An index of everything under `docs/`. Start here to find the right document
before reading further — see [`AGENTS.md`](../AGENTS.md) for which documents
are *mandatory* reading before writing or modifying code.

| File | Summary | Read this when... |
| --- | --- | --- |
| [`roadmap.md`](roadmap.md) | High-level, dependency-ordered delivery sequence: scaffolding → walking-skeleton review loop → RAG → verification → Memory → admin UI (MVP line) → multi-org tenancy → knowhow augmentation → hardening, plus what's deliberately deferred. | Deciding what to build next, checking whether a feature is in-scope for MVP, or understanding why one capability is sequenced before another. |
| [`rules/architecture.md`](rules/architecture.md) | Mandatory architecture rules: DDD package structure by bounded context, `typing.Protocol` decoupling boundaries, adapter/domain/entrypoint layering, simplicity constraints (YAGNI, two deployables not five). Ends with verification tag `WKPYNQ`. | Before writing or modifying **any** code in this repo — mandatory per `AGENTS.md`. Also when deciding where new code should live, how to add a new adapter (e.g. GitLab alongside GitHub), or whether a new abstraction is justified. |
| [`rules/testing.md`](rules/testing.md) | Mandatory testing rules: the unit/integration/E2E pyramid mapped onto `architecture.md`'s packages and pipeline stages, the PRD → user story → integration test traceability requirement, and CI gating per test tier. Ends with verification tag `RQPTVE`. | Before writing or modifying any code (mandatory per `AGENTS.md`), and mandatory on its own before drafting or reviewing any PRD/spec — it defines how user stories must map to integration tests. |
| [`reference/c4-architecture.md`](reference/c4-architecture.md) | The working C4 system architecture: containers/services (Webhook API, Worker, LLM Gateway, Postgres/pgvector), the design goals that shaped it (self-hosted, bring-your-own-LLM, real indexing, human-validated learning), and why it replaced an earlier "zero infrastructure" CLI design. | Starting point for understanding the system's shape — what services exist, how they talk to each other, and why. Read before designing any change that spans more than one service/package. |
| [`reference/rag-techniques.md`](reference/rag-techniques.md) | Technique-level reference on indexing/RAG mechanics: chunking strategies (naive vs. boundary-aware vs. AST-aware/cAST), embedding, storage, and retrieval, cross-referencing what the vendor research found and what a sibling project (`knowhow`) does. | Designing or evaluating anything touching the Indexing job or Context Builder — chunking strategy, embedding choices, retrieval tuning. |
| [`reference/operations.md`](reference/operations.md) | Operator-facing runbook: diagnosing a restart-looping Worker after an upgrade (a fail-fast startup check, not a crash), the per-repo review-enqueue rate limiter's disabled-by-default state, and running `alembic upgrade head` manually before a Helm upgrade that includes new migrations. | Deploying or upgrading a real instance of this platform, or diagnosing an operator-reported issue after rollout. |
| [`reference/axi.md`](reference/axi.md) | Summary of the AXI (Agent Experience Interface) design principles for building agent-ergonomic CLIs — token-efficient output, minimal schemas, structured errors, contextual disclosure — with reported cost/success benchmarks vs. MCP-based equivalents. | Designing any CLI, tool output format, or agent-facing interface in this platform and want established patterns for making it cheap and reliable for an LLM agent to consume. |
| [`research/research-current-art.md`](research/research-current-art.md) | Primary research survey of how commercial AI code review tools (CodeRabbit, Greptile, Qodo, Bito, Sourcery, GitHub Copilot, Snyk, Semgrep, etc.) actually achieve thorough review — indexing/RAG, static-analysis-first hybrids, multi-pass pipelines, verification/judge passes, feedback loops. This is the foundational document the rest of `docs/` derives from. | Before questioning *why* the architecture is built this way, when evaluating whether a new technique is worth adopting, or when you need concrete vendor evidence/benchmarks to justify a design decision. |
| [`adr/0001-self-hosted-kubernetes-deployment.md`](adr/0001-self-hosted-kubernetes-deployment.md) | ADR: platform ships as container images the customer runs themselves (Helm/Kubernetes in production, Docker Compose locally), reversing an earlier zero-infrastructure (git-hook/CI, no server, no database) design. | Understanding why the platform has a database and always-on services at all, or when a change seems to reintroduce a "stateless CLI" assumption that no longer holds. |
| [`adr/0002-implement-in-python.md`](adr/0002-implement-in-python.md) | ADR: services implemented in Python with LiteLLM as the LLM Gateway's routing layer, superseding an earlier Go decision now that ADR-0001 removed the bare-metal fast-path constraint that motivated Go. | Understanding why the codebase is Python (not Go), or before proposing a language/runtime choice for a new service. |
| [`adr/0003-knowhow-product-knowledge-augmentation.md`](adr/0003-knowhow-product-knowledge-augmentation.md) | ADR: review owns its own code indexing/RAG pipeline outright; the separate `knowhow` product (Confluence/Slack/tickets) is an optional MCP-based augmentation only, never a shared database or in-process dependency. | Working on the Context Builder's integration with knowhow, or deciding whether new functionality belongs inside review's own indexing pipeline vs. as an external knowhow call. |
| [`adr/0004-multi-org-tenancy.md`](adr/0004-multi-org-tenancy.md) | ADR: one deployment supports multiple GitHub orgs, but modeled as one customer's internal structure (business units/internal teams) — org is a scoping/config key alongside repo and team, not a hard multi-tenant security boundary between unrelated companies. | Working on the tenancy/RBAC data model, org-level scoping for findings/Memory/config, or deciding whether a feature needs true multi-tenant isolation (it explicitly doesn't provide that — see the ADR's trade-offs). |
| [`adr/0005-pure-no-io-logic-unit-tested.md`](adr/0005-pure-no-io-logic-unit-tested.md) | ADR: pure, no-I/O logic (e.g. `adapter/github/triggers.py`'s lookup table) is Unit-tested regardless of which PRD user story it serves or which package it's physically implemented in — a narrow, named exemption from `testing.md`'s story→integration-test rule. | Deciding whether a new pure/no-I/O function needs an Integration test just because its story or package would otherwise imply one — check this ADR's scope list first. |
| [`adr/0006-protocol-defined-at-data-model.md`](adr/0006-protocol-defined-at-data-model.md) | ADR: a `typing.Protocol` may be defined alongside the data model it produces (e.g. `ingestion.diff.DiffFetcher` in `ingestion/diff.py`) or substantively operates on (e.g. `findings.redact.TextRedactor` in `findings/redact.py`), not only at its first consumer — a narrow, named exemption from `architecture.md`'s "defined at the consumer" rule, only when more than one consumer would otherwise import it from an arbitrarily-chosen sibling module. | Deciding where to define a new Protocol with more than one real consumer — check this ADR's scope list first before defaulting to "the first consumer that needs it." |

## How to update this index

Update this file whenever a document is added, removed, renamed, or
substantially repurposed under `docs/`.

- **Adding a file:** add one row to the table above, in the same directory
  grouping order already used (`rules/`, then `reference/`, then
  `research/`, then `adr/`). The **Summary** column should compress what the
  document actually contains — its core claims/decisions/rules, not a
  restatement of its title. The **Read this when...** column should state
  the concrete trigger for opening it (a task, a question, a type of change)
  rather than a vague "for context."
- **Removing or renaming a file:** delete or update its row; fix any other
  row's relative link that pointed at it.
- **Rewriting a file's content:** re-derive its Summary/Read-this-when cells
  from the new content — don't leave a stale summary describing the old
  version.
- Keep rows one-per-file. If a document is split into multiple files, give
  each its own row.
- Do not add commentary, changelogs, or a "recent updates" section to
  this file — it is a point-in-time index of current documentation, not a
  history. Git history already covers that.
