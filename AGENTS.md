# AGENTS.md

## Project Overview

**review** is a self-hosted, enterprise AI code review platform — a genuine
competitor to CodeRabbit, Qodo, and Greptile, not a personal cost-avoidance
script. It ships as container images the customer deploys inside their own
infrastructure (Kubernetes via Helm in production, Docker Compose locally),
so the customer controls where code and review data go: nothing is sent to the
project maintainers, and the only destinations are the services the customer
configures (GitHub, an LLM endpoint of their choice, and optional providers).

The architecture is deliberately derived from research into how the leading
commercial tools actually achieve thorough review
([`docs/research/research-current-art.md`](docs/research/research-current-art.md)),
and is built to match or beat them on the techniques that matter, not just
ship a diff-into-an-LLM wrapper:

- **Real indexing/RAG, not a bare diff** — AST-aware chunking, embeddings,
  and a call/import graph kept incrementally fresh on every push (see
  [`docs/reference/c4-architecture.md`](docs/reference/c4-architecture.md),
  [`docs/reference/rag-techniques.md`](docs/reference/rag-techniques.md)).
- **Static analysis first** as the free, deterministic trust anchor an
  LLM's own findings get checked against, before any LLM inference runs.
- **Structurally separate verification, never self-critique** — the
  research's most consistently reproduced finding across vendors is that
  asking a model to re-rate its own output is close to random.
- **Continual, human-validated learning** — the platform learns from the
  reviews it performs (dismissed findings, accepted fixes, developer
  feedback on posted comments), but every learning is surfaced for a human
  to approve or remove before it reshapes future reviews, never a silent
  black box.
- **Bring-your-own-LLM, any endpoint** — the platform never proxies
  inference or holds provider credentials of its own.
- **Optional augmentation, never a dependency** — a separate product
  (knowhow) can be connected for non-code product knowledge (Confluence,
  Slack, tickets); its absence never degrades review's core capability
  (see [`docs/adr/0003-knowhow-product-knowledge-augmentation.md`](docs/adr/0003-knowhow-product-knowledge-augmentation.md)).

Start with [`docs/reference/c4-architecture.md`](docs/reference/c4-architecture.md)
for the system architecture and [`docs/adr/`](docs/adr/) for the reasoning
behind its major decisions.

## Mandatory reading

Before writing or modifying any code in this repository, read [`docs/rules/architecture.md`](docs/rules/architecture.md) and [`docs/rules/testing.md`](docs/rules/testing.md) in full — not a summary, not an assumption based on filename or prior sessions. Before drafting or reviewing a PRD/spec, `docs/rules/testing.md` is mandatory reading on its own — it defines how user stories must map to integration tests.

Each of those documents ends with a **verification tag**. Before starting any implementation, PRD, or spec work, state the tag(s) verbatim for whichever document(s) apply. If you cannot produce a tag, you have not actually read that file — go read it now, then state the tag.

Do not proceed with code changes or PRD/spec work until the applicable tag(s) have been stated.
