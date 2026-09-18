# Roadmap

A high-level view of delivery order: what gets scaffolded first, what
capability lands in each phase, and what's deliberately deferred. This is a
sequencing document, not a project plan — no dates, no tickets. It answers
"what comes before what and why," not "who does what by when." For the
system being built, see [`reference/c4-architecture.md`](reference/c4-architecture.md);
for why it's built this way, see [`adr/`](adr/) and
[`research/research-current-art.md`](research/research-current-art.md).

Phases are dependency-ordered, not strictly sequential — later phases can
start once their prerequisites land, without waiting for every earlier phase
to be "complete."

---

## Phase 0 — Scaffolding

The non-feature foundation every later phase builds on. Nothing here is
customer-visible.

- Repo structure per [`rules/architecture.md`](rules/architecture.md): domain
  packages (`ingestion`, `indexing`, `review`, `llmgateway`, `findings`,
  `memory`, `tenancy`), adapter packages, the two entrypoints
  (`apps/webhook_api`, `apps/worker`).
- CI: `ruff` + strict type checker gate, unit test run on every push (per
  [`rules/testing.md`](rules/testing.md)'s CI-gating section).
- Docker Compose stack for local dev (Webhook API + Worker + Postgres/pgvector,
  LLM Gateway pointed at local LM Studio) — the same images Kubernetes runs in
  production ([ADR-0001](adr/0001-self-hosted-kubernetes-deployment.md)).
- Postgres schema baseline (Alembic): org/repo/team tables, job queue
  (`SKIP LOCKED`-style), bootstrap config tables.
- LLM Gateway skeleton: LiteLLM wired in, one provider configured end-to-end
  ([ADR-0002](adr/0002-implement-in-python.md)).
- GitHub App skeleton: webhook signature verification, installation
  resolution, App manifest/callback.
- Helm chart skeleton: `ConfigMap`/`Secret` rendering, `existingSecret`
  support, `postgresql.enabled` toggle — doesn't need to be production-grade
  yet, needs to exist so later phases deploy the same way from day one.

**Exit criteria:** `docker compose up` runs a Webhook API + Worker + Postgres
that boots cleanly with no review capability yet.

---

## Phase 1 — Walking skeleton: the core review loop

Prove the full pipeline end-to-end with the simplest possible version of
each stage before adding sophistication to any one of them.

- Webhook API receives a PR event, enqueues a review job.
- Worker: pre-filter → **bare diff** (no index yet) → one LLM call → findings
  posted as inline PR comments + check-run status via the GitHub Adapter.
- Static analysis runner integrated as the free, deterministic first pass
  (design goal 4 — this ships *before* RAG sophistication, not after, because
  it's free and it's the trust anchor everything else gets checked against).

**Exit criteria:** a real PR against a real repo gets a real (if
unsophisticated) review posted by the platform.

---

## Phase 2 — Real indexing / RAG

Replace "bare diff" with the structural context every credible competitor
builds (research §2.1).

- Incremental indexing job: AST-aware chunking, per-node embeddings,
  call/import graph — kept fresh on every push. LLM-free by design — bi-encoder
  embeddings and static parsing only, no chunk-level summary generation,
  hierarchical summarization, or LLM-based entity extraction (see
  [ADR-0007](adr/0007-llm-free-indexing-deferred-rag-enrichment.md)).
- Context Builder queries the index during review (blast-radius lookups for
  changed signatures, not just the changed hunk).
- Review job upgraded to consume built context instead of the bare diff from
  Phase 1.

---

## Phase 3 — Verification / multi-pass pipeline

Add the structurally-separate check that the research (and design goal 6)
identifies as the difference between "thorough" and "diff-into-an-LLM."

- Multi-pass pipeline: intent summary → review generation → verification.
- Verification runs as a distinct call against a fixed rubric — never
  self-critique.
- Findings merger: dedupes, applies confidence/severity, drops low-value
  findings before a human sees them.

---

## Phase 4 — Memory (continual learning, human-validated)

- Memory schema: `pending`/`approved` status, provenance, ranking-time decay
  (half-life, steeper for `pending` — see the Memory bullets in
  [`reference/c4-architecture.md`](reference/c4-architecture.md)).
- GitHub Adapter's write path: captures reactions/replies/thread-resolution
  on the platform's own posted comments as candidate Memory entries.
- Findings merger & verify start *reading* approved Memory entries (design
  goal 6 — reads only what's been approved, never self-trusts).
- Admin API surface: list/approve/reject/delete, scoped by org/repo/team —
  API-only at this point, the UI catches up in Phase 5.

*Open: exact column-level schema for Memory entries and per-team rules, and
how a dismissal becomes the similarity-check signal design goal 6 commits
to — see `c4-architecture.md`'s "Open questions."*

---

## Phase 5 — Admin UI (MVP)

Confirmed in scope for the MVP release, not a fast-follow.

- Org/repo/team configuration screens.
- Memory approve/reject/delete screen, built directly on Phase 4's admin API.
- Model routing configuration (which endpoint/model serves which pipeline
  stage).

*Open: whether SSO/OIDC ships in this same MVP or immediately after — see
`c4-architecture.md`'s "Open questions."*

**— MVP line —** Phases 0–5 are what a pilot customer needs: a working
review loop, real context, verification, basic learning, and a UI to
configure and audit it. Everything below is real, accepted scope, but not
MVP-blocking.

---

## Phase 6 — Multi-org tenancy

Per [ADR-0004](adr/0004-multi-org-tenancy.md): org joins repo/team as a
scoping key, modeled as one customer's internal structure, not
multi-tenant-SaaS isolation between separate companies.

- Org-scoping layer across findings, Memory, and config.
- Multiple GitHub App installations under one deployment.
- RBAC: default visibility stays within an org; cross-org visibility is an
  explicit admin grant, not the default.

*Open: exact RBAC design for org-scoping — flagged in ADR-0004's
consequences and `c4-architecture.md`'s "Open questions."*

---

## Phase 7 — Optional external product-knowledge augmentation

Per [ADR-0003](adr/0003-knowhow-product-knowledge-augmentation.md) — entirely
optional, review is fully functional with none of this configured, and not
locked to any single provider.

- Define and version review's own product-knowledge MCP contract
  (`product_knowledge_search`/`product_knowledge_fetch`/
  `product_knowledge_related`).
- Context Builder's additional MCP client call against a configured
  provider's endpoint.
- Context fusion: the configured provider's results kept as a distinct,
  labeled source (labeled by provider display name) alongside review's own
  code-derived context.
- Per-org/repo provider endpoint + credential + display-label configuration.
- knowhow ships as the contract's reference implementation — the first, and
  for now only, provider — but is configured through the same generic path
  any future provider would use, with no knowhow-specific code in the
  Context Builder.

*Open: exact context-fusion/citation UX, review's own contract
versioning/deprecation policy, and whether a conformance test harness is
worth shipping for third-party implementers — all flagged in ADR-0003 and
`c4-architecture.md`.*

---

## Phase 8 — Production hardening

Ongoing, but concentrated once a real pilot customer is running the platform
in their own cluster rather than a demo Docker Compose stack.

- Helm chart maturity: health probes, resource defaults, upgrade paths,
  observability hooks — a first-class product surface per
  [ADR-0001](adr/0001-self-hosted-kubernetes-deployment.md), not an
  afterthought.
- Per-pipeline-stage model routing tuned for cost/quality (cheap model for
  triage, strongest configured model for review generation + verification).
- Strong local reproducibility (Docker Compose mirrors production images) to
  compensate for the trade-off ADR-0001 accepted: no shared fleet visibility
  across customers.

---

## Deferred, not scheduled

Real, deliberate scope — not forgotten, not designed away — but not on this
roadmap because nothing above depends on them landing first.

- **GitLab/Bitbucket adapters.** The git-host boundary in the domain layer is
  already designed so these don't touch pipeline logic when they do land.
- **Vendor-hosted SaaS offering.** Explicitly deferred by
  [ADR-0001](adr/0001-self-hosted-kubernetes-deployment.md) — may be added as
  an *additional*, not exclusive, offering once the self-hosted product is
  proven.
- **True multi-tenant isolation** (e.g. an MSP running one deployment for
  several separate client companies). [ADR-0004](adr/0004-multi-org-tenancy.md)
  explicitly does not provide this — would need revisiting, not a natural
  extension of Phase 6's model.
- **Optional, richer RAG enrichment** (docstring/summary embedding,
  RAPTOR-style hierarchical summarization, LLM-based entity/relationship
  graph extraction). Per [ADR-0007](adr/0007-llm-free-indexing-deferred-rag-enrichment.md),
  Phase 2's indexing job stays LLM-free by default to keep per-push indexing
  cost and latency low; these techniques land later, if ever, as an
  explicitly opt-in capability a customer configures, not a change to the
  default indexing path.

---

## How to update this roadmap

- Reorder or re-scope a phase when a real dependency changes, not to reflect
  optimism or slippage — this document sequences work, it doesn't track
  progress. Use issue tracking (Jira, GitHub Projects, whatever the team
  uses) for status; don't turn this into a Kanban board.
- When a phase's "Open" note gets resolved (an ADR lands, an open question in
  `c4-architecture.md` gets answered), remove the note — don't leave stale
  open-question callouts here once the source document has resolved them.
- A new phase gets a name, a one-line reason it's ordered where it is
  relative to its neighbors, and a short bullet list of what lands — not a
  full task breakdown. If you're writing ticket-level detail, it belongs in
  the tracker, not here.
- If a phase moves across the MVP line, update the "MVP line" callout in
  place rather than leaving two conflicting statements of what MVP includes.
- Keep the "Deferred, not scheduled" section for genuinely deliberate
  deferrals only (backed by an ADR or an explicit decision) — not a dumping
  ground for ideas nobody has committed to.
