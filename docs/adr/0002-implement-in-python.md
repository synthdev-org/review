# ADR: Implement the platform in Python

## Status

ACCEPTED

## RACI

| Who | Role | RACI |
| --- | --- | --- |
| Andy Beak | Author / sole engineer | R, A |

## Executive Summary

The platform's services (Webhook API, Worker) are implemented in Python,
using [LiteLLM](https://github.com/BerriAI/litellm) as the LLM Gateway's
routing layer to satisfy the hard requirement that the platform must be able
to point at any LLM endpoint — a local LM Studio instance in development, any
hosted or self-hosted API in production. An earlier decision to build this
project in Go is superseded: the constraint that made Go the clear choice
then (avoiding Docker on a bare local git-hook fast path) no longer exists
under [ADR-0001](0001-self-hosted-kubernetes-deployment.md)'s deployment
model, and the constraint that now dominates (native multi-provider LLM
routing) favors Python.

## Context

The project's original language ADR chose Go primarily because the
architecture at the time required a single binary that could run instantly
from a local git hook with zero dependency drift, and this project owner's
own standing tooling rule mandates running Python only inside Docker, never
as a local binary or venv — a rule that would have forced every local
git-hook invocation through a container, undermining the point of a fast
pre-commit check. That constraint is gone: under ADR-0001, the platform runs
exclusively as containerized services — Docker Compose in development,
Kubernetes in production — for every environment, by design. There is no
bare-metal fast path left to protect, so running Python via Docker in both
environments is exactly what the project owner's own rule already prescribes,
not something it conflicts with.

At the same time, the C4 architecture's design goals now include a hard
requirement that didn't previously exist in this form: the LLM Gateway must
be able to point at *any* configured endpoint — OpenAI-compatible local
servers (LM Studio, vLLM, TGI), any hosted API (Anthropic, OpenAI, Azure
OpenAI, Bedrock), all bring-your-own-key. Go has no equivalent to LiteLLM;
the original Go ADR already flagged this as a real, hand-built,
self-maintained cost when the LLM Gateway was a secondary concern behind a
free-static-analysis-first local tool. It is no longer secondary — it is the
platform's core differentiator for a self-hosted product whose customers may
run anything from Anthropic's API to an air-gapped local model server.

## Options considered

| Option | LLM multi-provider routing | Indexing/RAG ecosystem | Concurrency fit | Distribution (now containerized everywhere) | Verdict |
| --- | --- | --- | --- | --- | --- |
| **Python** | LiteLLM natively covers 100+ providers, including local OpenAI-compatible servers — directly satisfies the hard requirement with a maintained library | Richest available: mature tree-sitter bindings, embedding/RAG tooling, pgvector client, FastAPI/SQLAlchemy/Alembic for the surrounding service | asyncio is adequate — this workload is I/O-bound (subprocess exec for analyzers, HTTP calls to LLM/GitHub APIs), not CPU-bound, so the GIL isn't the constraint it would be for compute-heavy work | No advantage lost: single-binary distribution stopped mattering once nothing runs outside a container; image size/cold start are real but secondary for long-running, horizontally-scaled service pods | **Accept** |
| **Go** | No LiteLLM equivalent — multi-provider routing is hand-rolled and self-maintained across every provider the platform must support, now a core rather than incidental cost | Thinner AST/RAG tooling than Python's | Goroutines are a clean fit for parallel worker pools, but no longer the deciding factor since the workload is I/O-bound either way | Smaller images, faster cold start — real but non-decisive now that pods are long-running, not instantly-invoked git hooks | Reject — its original decisive advantage (avoiding Docker on a bare git-hook path) no longer exists, and the newly dominant requirement (LLM routing) now favors Python |
| **Rust** | No equivalent to LiteLLM | Thin — no compensating advantage over Python here | Strong, but no clearer advantage than Python for an I/O-bound orchestration workload | Same as Go — real but non-decisive now | Reject — same reasoning as the original ADR, unchanged by the new requirements |
| **TypeScript/Node** | Vercel AI SDK covers multiple providers but with less comprehensive local/self-hosted-endpoint coverage than LiteLLM | Weaker than Python's for AST/embedding tooling | Adequate (event loop), no advantage over Python | Same packaging-drift weakness noted in the original ADR, now moot since everything ships as containers regardless | Reject — no compensating advantage over Python for the now-dominant requirement |

## Decision

Build the platform's services in Python: FastAPI for the Webhook API's HTTP
surface, the same runtime for the Worker, SQLAlchemy + Alembic for
Postgres/pgvector access, pydantic-settings for config parsed once at each
entrypoint, and LiteLLM as the LLM Gateway's provider-routing layer.

## Consequences

**Easier / positive:**

* LiteLLM directly satisfies the platform's core "point the LLM anywhere"
  requirement with an actively maintained library across 100+ providers,
  instead of a hand-rolled adapter set we would own indefinitely.
* Richest available ecosystem for the indexing/RAG pipeline (AST parsing,
  embeddings, pgvector integration) that ADR-0001's requirements depend on.
* FastAPI + pydantic + SQLAlchemy + Alembic is a mature, widely understood
  stack for exactly this shape of service (webhook API + worker + Postgres),
  easing future hiring/onboarding beyond a solo maintainer.
* No longer in tension with this project owner's own Python-via-Docker
  tooling rule — the containerized-everywhere deployment model means that
  rule is satisfied by construction, not worked around.

**Harder / trade-offs:**

* Weaker static-typing guarantees than Go by default — mitigated by
  requiring strict `mypy`/`pyright` in CI and pydantic models validating
  every boundary (HTTP payloads, job payloads, config), per the rules in
  [`architecture.md`](../rules/architecture.md).
* The GIL limits true CPU parallelism within a single process — judged
  acceptable because the workload is I/O-bound (subprocess exec, HTTP);
  any genuinely CPU-heavy step (e.g. local embedding computation, if ever
  needed instead of calling an embeddings API) would run in a separate
  process pool rather than assuming in-process parallelism.
* Larger container images and slower cold start than a Go static binary —
  acceptable because Worker and Webhook API are long-running,
  horizontally-scaled service pods, not instantly-invoked git hooks; the
  constraint that made cold-start decisive in the original ADR is gone.

## Related

* [`docs/reference/c4-architecture.md`](../reference/c4-architecture.md) — the
  architecture this decision implements, including the LLM Gateway's
  multi-provider requirement.
* [`docs/adr/0001-self-hosted-kubernetes-deployment.md`](0001-self-hosted-kubernetes-deployment.md)
  — the deployment-model decision that removed Go's original deciding
  advantage.
* [`docs/research/research-current-art.md`](../research/research-current-art.md)
  — research into how existing tools achieve thorough review, which grounded
  the architecture both ADRs serve.
