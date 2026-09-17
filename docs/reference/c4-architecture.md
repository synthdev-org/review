# C4 Architecture — Self-Hosted Enterprise Code Review Platform

This is the working architecture for a self-hosted AI code review platform intended
as a genuine competitor to hosted tools like CodeRabbit, Qodo, and Greptile — not a
personal cost-avoidance script. It's derived from [`docs/research/research-current-art.md`](../research/research-current-art.md),
which surveyed how those tools actually achieve thorough review, plus a deliberate
choice to build an installable enterprise product rather than a service we run for
customers: the platform ships as container images the customer deploys inside
their own infrastructure, so their code and review history never leave their
control.

This document replaces an earlier "zero infrastructure" design (a CLI invoked as a
git hook / CI step, with no database and no server). That design was abandoned once
it became clear it couldn't deliver two of the highest-leverage techniques the
research identified: real indexing/RAG (§2.1) and persistent, team-shared memory
(§2.4). Both require durable, queryable, shared state — which a stateless CLI
can't hold, and which can't be faked by committing files into the customer's
application repo without forcing every teammate who doesn't use the tool to carry
files they don't need.

## Design goals that shaped this architecture

1. **Self-hosted, not vendor-hosted.** The platform is a product the customer
   installs and runs in their own infrastructure — production target is
   Kubernetes via an official Helm chart; local development runs the same
   container images via Docker Compose. We never see the customer's code or
   review data. (See [ADR-0001](../adr/0001-self-hosted-kubernetes-deployment.md).)
2. **Real, persistent, shared state — owned by the customer, not us.** Because
   the customer runs their own deployment, a real database is no longer
   something to avoid; it's the natural place to put the index and the team's
   shared memory. Postgres + pgvector runs inside the customer's own
   deployment — either a bundled Helm subchart (`postgresql.enabled: true`,
   for quick trials/demos) or their own externally managed instance
   (`postgresql.enabled: false` plus connection details, for production) —
   never inside the reviewed repository.
3. **Any LLM, anywhere, bring-your-own-key.** The platform never calls an LLM
   on the customer's behalf and never proxies inference through infrastructure
   we run. A developer's local machine (Docker Compose) points the LLM
   Gateway at a local LM Studio instance; production points it at whatever
   endpoint the customer configures — a hosted API (Anthropic, OpenAI, Azure
   OpenAI, Bedrock), a self-hosted model server (vLLM, TGI), or anything else
   speaking an OpenAI-compatible or Anthropic-compatible wire format.
4. **Free layer before paid layer.** Static analysis, linting, and secret
   scanning still run first, cost nothing, and remain the trust anchor the
   LLM's own findings get checked against — the research's most consistently
   converged-on pattern (§2.2), and unaffected by any of the above changes.
5. **Multi-org tenancy, scoped to one customer's internal structure.** One
   deployment can connect multiple GitHub orgs — the intended shape is a
   single enterprise customer running several internal orgs (business units,
   internal teams), not unrelated companies sharing infrastructure. Org sits
   alongside repo and team in the tenancy hierarchy (org → repo → team →
   findings/memory) as a scoping/config key, not a hard multi-tenant security
   boundary — see [ADR-0004](../adr/0004-multi-org-tenancy.md). Scoped for
   GitHub only in this iteration; the git-host boundary is designed so
   GitLab/Bitbucket can be added without touching domain code.
6. **Self-critique doesn't work; a structurally separate check does.** Kept
   from the original research: Greptile, Sourcery, and Qodo each independently
   found that asking a model to re-rate its own output is near-random.
   Verification always runs as a distinct call against a fixed rubric or a
   similarity check against past-dismissed findings — never a "please
   double-check yourself" follow-up to the same call.
7. **Secret-backend-agnostic.** The platform never assumes a specific secret
   manager. Every customer Kubernetes environment already has one (Vault,
   AWS/Azure/GCP Secrets Manager, External Secrets Operator, Sealed Secrets,
   or similar) — the Helm chart's job is to consume a `Secret` object, not to
   pick which tool populates it. See "Configuration & Secrets" below.
8. **Optional augmentation, never a dependency.** Review owns its full code
   indexing pipeline outright — chunking, embeddings, and the call/import
   graph never depend on any other system, and the platform is fully
   functional with none configured. A separate, independently-deployed
   product — knowhow, a general-purpose multi-source RAG knowledge base
   (Confluence, Slack, tickets, and more) — can optionally be connected over
   its MCP API to add non-code product knowledge to review context. Its
   absence is a normal, fully supported deployment state, not a degraded
   one. (See [ADR-0003](../adr/0003-knowhow-product-knowledge-augmentation.md).)
9. **Continual learning must be human-validated and reversible, not silent.**
   The platform is meant to get better at reviewing a given codebase over
   successive PRs — the pattern every mature vendor converges on independently
   (§2.4) — but a machine-inferred learning only earns the trust future
   reviews place in it once a human has seen and approved it. Every learning
   carries a status (inferred/pending → approved/rejected), its provenance
   (the finding, PR, or comment it came from), and stays visible and
   deletable in the platform's own admin surface for as long as the customer
   keeps it — never a black box silently reshaping future reviews. This is
   the write-side counterpart to design goal 6: verification *reads* Memory
   and never trusts itself; this goal governs how Memory gets *written to*,
   and who gets to keep or remove what lands there.

---

## Level 1 — System Context

```
┌────────────────────────┐          ┌──────────────────────────────┐
│  Developer's Machine     │          │  GitHub (customer's org)       │
│  docker compose up:      │          │  repos, PR webhooks, checks    │
│  webhook-api + worker +  │          └───────────────┬────────────────┘
│  postgres, LLM → LM       │                          │ webhook: PR opened/
│  Studio (localhost)       │                          │ synchronized/comment
└──────────┬────────────────┘                          ▼
           │ same images                 ┌──────────────────────────────┐
           │ as production                │   Review Platform               │
           │                               │   (customer's own Kubernetes    │
           │                               │   cluster, Helm-deployed)       │
           │                               └───────────────┬──────────────────┘
           │                                                │ posts inline PR
           │                                                │ comments + check-run
           │                                                │ status, via GitHub
           │                                                │ App install token
           │                                                ▼
           │                                   (back to GitHub, same box above)
           │
           └────────────────────────► ┌──────────────────────────┐
                                        │   LLM Provider(s)          │
                                        │  any endpoint the customer  │
                                        │  configures — local model,  │
                                        │  hosted API, self-hosted     │
                                        │  inference server (BYO key)  │
                                        └──────────────────────────┘

Platform admin (customer's own platform/security team) configures GitHub App
credentials, LLM endpoint(s), and per-team settings via Helm values + the
platform's own config store — never via files committed into a reviewed repo.
```

**Reading this diagram:** there are two *required* systems the platform talks
to — GitHub (the git host, scoped to GitHub only for this iteration) and an
LLM Provider (intentionally drawn as an interchangeable external system,
since which provider serves a given call is entirely a customer runtime-config
choice — covered in the Level 3 diagram). The platform itself is one system
with one architecture, deployed two ways: Docker Compose for local
development, Kubernetes/Helm for production. There is no vendor-hosted
counterpart.

A third, *optional* external system is deliberately omitted from the diagram
above because nothing in the platform depends on it: **knowhow**, a separate
self-hosted product a customer may also run. When configured, the Worker's
Context Builder calls knowhow's MCP API to pull in non-code product knowledge
(Confluence, Slack, tickets) alongside review's own code-derived context — see
Level 2 and [ADR-0003](../adr/0003-knowhow-product-knowledge-augmentation.md).

---

## Level 2 — Container

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    REVIEW PLATFORM (customer's own deployment)                │
│                                                                                │
│  ┌───────────────────────┐                                                   │
│  │ Webhook API              │  FastAPI service. Verifies GitHub webhook        │
│  │ (stateless, scales        │  signature, resolves the installation/org/repo,  │
│  │  horizontally)             │  enqueues a review or reindex job. Also serves   │
│  │                             │  the GitHub App callback + minimal admin/config │
│  │                             │  endpoints.                                      │
│  └───────────┬─────────────────┘                                                │
│              │ enqueues                                                         │
│              ▼                                                                  │
│  ┌───────────────────────┐                                                     │
│  │ Job Queue                │  Postgres-backed (row-level locking / SKIP        │
│  │ (in Postgres — no          │  LOCKED), not a separate broker — one fewer       │
│  │  separate broker)          │  moving part the customer has to operate.        │
│  └───────────┬─────────────────┘                                                │
│              ▼                                                                  │
│  ┌────────────────────────────────────────────────────────────┐               │
│  │ Worker (horizontally scalable Deployment; both job types)      │               │
│  │                                                                  │               │
│  │  Indexing job:                     Review job:                  │               │
│  │  ┌─────────────────────┐          ┌─────────────────────────┐ │               │
│  │  │ Incremental indexer    │          │ 1. Pre-filter              │ │               │
│  │  │ — AST chunking, per-    │          │ 2a. Static analysis runner │ │               │
│  │  │   node embeddings,      │          │ 2b. Context builder         │ │               │
│  │  │   call/import graph     │          │    (queries the index)      │ │               │
│  │  │   (writes to Postgres/  │          │ 3. LLM Gateway               │ │               │
│  │  │   pgvector)             │          │ 4. Findings merger & verify  │ │               │
│  │  └─────────────────────┘          │    (checks Memory store too) │ │               │
│  │                                     └────────────┬─────────────────┘ │               │
│  │                                                    │ posts results       │               │
│  │                                                    ▼                     │               │
│  │                                          GitHub Adapter (inline PR       │               │
│  │                                          comments + check-run status)    │               │
│  └────────────────────────────────────────────────────────────────┘               │
│              │ reads/writes                                                     │
│              ▼                                                                  │
│  ┌───────────────────────────────────────────────────────────────┐            │
│  │ Postgres + pgvector                    (the one stateful dependency)          │
│  │  — repo index (AST chunks, embeddings, call/import graph)                     │
│  │  — findings history                                                            │
│  │  — Memory (per-team learnings: auto-inferred + explicitly taught;              │
│  │    each entry carries an approval status + provenance — design goal 9)          │
│  │  — job queue, org/repo/team config, RBAC                                       │
│  │  postgresql.enabled: true → bundled subchart | false → customer's own DB      │
│  └───────────────────────────────────────────────────────────────────┘            │
└─────────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                  ┌───────────────────────┐
                  │  LLM Gateway (library)   │  used by Worker only — see Level 3
                  └───────────────────────┘
```

**Reading this diagram:**

- **Two deployable components, not five.** Webhook API and Worker are the only
  two container images to build, scan, and patch — indexing and review are
  both job types the same Worker pool handles, dispatched by job type, rather
  than separate services. Fewer images is fewer things for an enterprise
  security team to have to approve and for us to operate.
- **No broker beyond Postgres.** The job queue lives in the same Postgres
  instance as everything else (`SKIP LOCKED`-style claiming), rather than
  requiring the customer to also stand up Redis or Kafka. This can move behind
  a real queue interface later if throughput demands it — the point is not
  requiring it on day one.
- **Memory now lives in the platform's own database, not the repo.** This
  directly answers the two problems zero-infrastructure couldn't solve:
  teammates who don't use the platform never see any files in their checkout
  (nothing is committed to the reviewed repo), and every teammate's review
  benefits from what every other teammate's feedback taught the system,
  because it's one shared store, not a per-machine local cache.
- **Memory has a write path, not just a read path.** The GitHub Adapter is
  bidirectional: alongside posting inline comments and check-run status, it
  also captures inbound signal on the platform's own posted comments —
  reactions, replies, and thread-resolution status — the same raw material
  Greptile and Cloudflare mine for continual learning (§2.4). A dismissed
  finding, an accepted fix, or a repeated correction becomes a candidate
  Memory entry with `pending` status, not an immediate rule; findings merger
  & verify still only *reads* approved entries during review (design goal
  6). Promoting a pending entry to `approved`, or deleting it outright, is a
  human action exposed through a Memory-specific slice of the platform's
  admin API (list/approve/reject/delete by org/repo/team) — the first
  concrete scope the broader admin UI (committed to the MVP release, see
  "Open questions" below) builds out from. (See design goal 9.)
- **Memory ages via ranking-time decay, never silent deletion.** Following
  the model already proven in the sibling `knowhow` project (`search/domain`'s
  `RankingPolicy`): staleness is a query-time confidence weight — exponential
  half-life decay from an entry's relevant timestamp — not a retention/expiry
  job that deletes rows. An old, unused learning simply ranks lower in future
  context bundles over time; it never vanishes on its own. The only way a
  Memory entry is removed is the human approve/reject/delete action design
  goal 9 already requires. Half-life is shorter for `pending` (auto-inferred,
  unreviewed) entries than for `approved` ones, mirroring knowhow's steeper
  decay for lower-trust content — so an unreviewed inference naturally fades
  out of relevance if nobody ever approves it, without requiring a human to
  actively reject it just to stop it accumulating influence.
- **The index is real RAG, not a workaround.** AST-based chunking, per-node
  embeddings, and a call/import graph — the same shape Greptile and CodeRabbit
  build (§2.1) — kept incrementally up to date by the indexing job on every
  push, queried by the Context Builder during review.
- **The indexing job makes zero generative LLM calls.** Chunk embeddings use
  a bi-encoder embedding model call (not generative), and the call/import
  graph is built by static parsing only — no docstring/summary generation,
  hierarchical summarization, or LLM-based entity extraction on the default
  indexing path. This keeps per-push indexing cost and latency low and,
  since design goal 3 makes every LLM call a customer-billed BYO-key call,
  keeps indexing free of inference spend the customer never asked for. Richer
  enrichment techniques are deferred as an optional, explicitly opt-in
  capability — see [ADR-0007](../adr/0007-llm-free-indexing-deferred-rag-enrichment.md).
- **Context Builder can optionally call out to knowhow.** After querying
  review's own index, the Context Builder makes an additional MCP client call
  to knowhow (`knowhow_search`/`knowhow_fetch`/`knowhow_related`) when a
  knowhow endpoint is configured for the org/repo, pulling in non-code
  product knowledge (Confluence, Slack, tickets). Review's own code-derived
  context and knowhow's product-knowledge results are kept as distinct,
  labeled sources in the bundle handed to the LLM Gateway, never silently
  merged — the two carry different trust/freshness characteristics. This
  call is skipped entirely, with no error and no degraded behavior, when no
  knowhow endpoint is configured. See
  [ADR-0003](../adr/0003-knowhow-product-knowledge-augmentation.md).
- **Config is customer-owned platform state, not repo files.** Helm values set
  it up at install time; day-two changes go through the platform's own
  config/admin surface and land in Postgres — never as files the reviewed
  repo's contributors have to see or ignore.
- **Postgres is bring-your-own-or-bundled, not bring-your-own-only.** The Helm
  chart's `postgresql.enabled` flag bundles a subchart (Postgres + the
  `pgvector` extension pre-installed) for a fast eval/trial path, defaulting
  **off** in the production values profile. When `false`, the chart takes
  connection details for an externally managed instance and the Worker
  verifies the `pgvector` extension is present at startup, failing fast with
  a clear error rather than surfacing an obscure query failure later.

---

## Configuration & Secrets

Two different kinds of configuration exist, deliberately handled differently:

- **Bootstrap config** — what a pod needs before it can do anything: the
  Postgres connection (or the bundled-subchart toggle), LLM Gateway default
  endpoint(s), GitHub App credentials (private key, webhook secret), log
  level, port. This is read once at each entrypoint (`apps/webhook_api`,
  `apps/worker`) via `pydantic-settings`, per `architecture.md`'s config
  rule — never read ad hoc from domain or adapter code.
- **Day-two platform config** — org/repo/team settings, per-stage model
  routing, rules, Memory, and — when the optional augmentation is enabled — a
  per-org/repo knowhow endpoint and credential
  ([ADR-0003](../adr/0003-knowhow-product-knowledge-augmentation.md)). This
  already lives in Postgres, managed through the platform's own admin API
  (see Level 2 above) — not a file, not an env var.

Bootstrap config is rendered by the Helm chart as a `ConfigMap` (non-secret
values) and a `Secret` (credentials), sourced from `values.yaml` for a quick
trial, and — critically — the chart also accepts an `existingSecret` name/key
reference for every credential field. That's what makes the platform
secret-backend-agnostic (design goal 7): whatever the customer already uses to
populate Kubernetes `Secret` objects — Vault, External Secrets Operator, a
cloud provider's Secrets Manager, Sealed Secrets, or their own tooling —
populates the `Secret` our chart reads, and the chart never has an opinion
about which one. We don't operate any secret-management infrastructure
ourselves, and no customer production pod ever needs outbound network access
to a third-party secrets SaaS we run, which would both break the
self-hosted trust story ([ADR-0001](../adr/0001-self-hosted-kubernetes-deployment.md))
and be a non-starter for air-gapped deployments. Locally, Docker Compose reads
the equivalent values from a plain `.env` file. (A developer is free to
source their own local `.env` from whatever tool they personally use — e.g.
`doppler run -- docker compose up` — that's a personal workflow choice
entirely outside the product's supported configuration contract.)

---

## Level 3 — Component (LLM Gateway)

```
┌────────────────────────────────────────────────────────────────────────┐
│                    LLM GATEWAY (component view, used by Worker)          │
│                                                                            │
│   ┌───────────────────┐                                                  │
│   │ Model Router         │  per-org/per-team, per-pipeline-stage routing   │
│   │ (config in Postgres,  │  table: which endpoint+model handles intent-    │
│   │  set via admin API)   │  summary vs. review-generation vs. verification │
│   └──────────┬───────────┘                                               │
│              ▼                                                           │
│   ┌────────────────────────────────┐                                    │
│   │  LiteLLM Router                    │  one call surface across every     │
│   │  (github.com/BerriAI/litellm)      │  configured endpoint — OpenAI-      │
│   │                                     │  compatible (LM Studio, vLLM,       │
│   │                                     │  TGI, Azure OpenAI, DeepSeek,       │
│   │                                     │  Groq, OpenAI) and Anthropic,       │
│   │                                     │  Bedrock, Vertex, 100+ others —     │
│   │                                     │  BYO key/endpoint only, never a     │
│   │                                     │  managed inference path we run.     │
│   └───────────┬─────────────────────┘                                    │
│               │                                                          │
│    ┌──────────┼──────────────────────────────┐                          │
│    ▼          ▼                              ▼                          │
│ Local dev:  Prod (example A):         Prod (example B):                 │
│ LM Studio   Anthropic API             self-hosted vLLM inside the       │
│ localhost   (customer's own key)      customer's own cluster            │
└────────────────────────────────────────────────────────────────────────┘
```

**Reading this diagram:**

- **LiteLLM is the reason Python won the language decision** (see
  [ADR-0002](../adr/0002-implement-in-python.md)) — "point the LLM anywhere"
  is a hard requirement, not a nice-to-have, and LiteLLM already implements
  exactly that routing surface across 100+ providers including local
  OpenAI-compatible servers, rather than requiring hand-rolled, self-maintained
  adapters.
- **Every environment uses the same router, different config.** A developer's
  `docker compose up` points at `http://host.docker.internal:1234` (LM
  Studio); production points at whatever endpoint(s) the customer's Helm
  values and Kubernetes Secrets specify. No code path distinguishes "local
  model" from "hosted API" — it's purely endpoint + credential configuration.
- **Routing is per pipeline stage, per team.** Point a cheap or local model at
  low-risk stages (intent summarization, static-findings triage) and reserve
  the strongest configured model for review generation and the verification
  pass, matching the research's finding that models "are no longer
  interchangeable" across sub-tasks (CodeRabbit) and that quality differences
  matter most at exactly those two stages.
- **BYO-key is a hard boundary, not a default.** The platform holds no
  provider credentials of its own and never brokers inference on the
  customer's behalf — every credential is supplied by the customer via
  Kubernetes Secret and used only to call the endpoint they specified.

---

## Open questions / not yet decided

- Scope and timing of SSO/OIDC for the admin UI. The admin UI itself is now
  committed to the MVP release, not a fast-follow (the Memory
  approve/reject/delete surface scoped by design goal 9 is its first
  concrete screen) — but whether SSO/OIDC ships in that same MVP or follows
  shortly after is still open. Whether this product joins the cross-product
  identity broker is separately resolved (yes, in principle — see
  `platform/docs/adr/0005-cross-product-identity-broker.md`'s 2026-09-10
  amendment): this product has no local login/user-identity model at all
  yet (no `user` table, no login code), which is the real prerequisite this
  bullet is blocked on, not just SSO timing.
- Exact schema for per-team rules/config and for Memory entries specifically
  — column-level design for status/provenance/approver/timestamps (design
  goal 9), and how a dismissal turns into the similarity-check signal design
  goal 6 already commits to using (§2.3). (Staleness/decay itself is now
  resolved — see the Memory bullet under Level 2 above.)
- Exact RBAC design for org-scoping under multi-org tenancy
  ([ADR-0004](../adr/0004-multi-org-tenancy.md)) — a team in one org
  shouldn't see another org's findings/Memory by default even though orgs
  aren't a hard multi-tenant security boundary there.
- GitLab/Bitbucket adapters are explicitly deferred, not designed away — the
  git-host boundary in the domain layer must not leak GitHub-specific shapes
  (webhook payload fields, check-run semantics) into pipeline logic.
- Exact context-fusion/citation UX for the optional knowhow augmentation —
  how dual-sourced (code vs. product-knowledge) context is presented to the
  review-generation LLM call, and how citations back to knowhow-sourced
  content should render in a posted PR comment
  ([ADR-0003](../adr/0003-knowhow-product-knowledge-augmentation.md)).
- Knowhow's MCP contract versioning/deprecation policy — needs confirming
  before the augmentation ships as a supported feature, not just a
  prototype.
