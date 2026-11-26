# Architecture Rules (Mandatory)

Rationale: [`c4-architecture.md`](../reference/c4-architecture.md),
[ADR-0001](../adr/0001-self-hosted-kubernetes-deployment.md) (deployment
model), [ADR-0002](../adr/0002-implement-in-python.md) (language). Deviation
requires a new ADR, not a one-off exception.

## Package structure (DDD)

- Organize by bounded context (`ingestion`, `indexing`, `review`, `llmgateway`,
  `findings`, `memory`, `tenancy`), never by layer (`controllers`, `models`,
  `utils`). One package, one responsibility — no `common`/`shared`/`utils`
  dumps.
- Domain packages: zero infra imports (no `fastapi`, `sqlalchemy`, `httpx`,
  `litellm`, `subprocess` — those live only in adapters). Adapters
  (`adapter/github`, `adapter/analyzers`, `adapter/storage`, `adapter/llm`)
  depend inward on domain-defined `typing.Protocol` interfaces only.
  Entrypoints (`apps/webhook_api`, `apps/worker`, `apps/admin_api`) depend
  on the domain and wire adapters in; the domain never imports entrypoint
  or adapter code.
- Three deployable entrypoints share every package below `apps/`: the
  Webhook API, the Worker (which itself dispatches both indexing and
  review jobs), and the Admin API (`apps/admin_api`, per
  [ADR-0001](../adr/0001-self-hosted-kubernetes-deployment.md)'s
  2026-09-20 amendment and `docs/specs/spec-admin-gui.md`) are thin
  composition roots, not separate codebases with duplicated logic.

## Decoupling

- Anything with >1 real or clearly upcoming implementation (LLM providers via
  LiteLLM, static analyzers, git hosts, output formats) sits behind a stable
  `typing.Protocol`, defined at the consumer (accept protocols, return
  concrete types) — swapping the implementation needs zero consumer changes.
- Each pipeline stage (pre-filter, static analysis, context builder, LLM
  gateway, findings merger, output) is its own package with a narrow
  interface and explicit pydantic models passed between stages — no shared
  mutable state, no stage reaching into another stage's internals.
- New adapter (e.g. a GitLab adapter alongside the GitHub one) = zero changes
  to domain or consumer code. If it requires one, the interface boundary is
  wrong — fix the boundary, don't special-case the new adapter.
- Wire concrete implementations only at the composition root
  (`apps/webhook_api/main.py`, `apps/worker/main.py`) via constructor
  injection. No module-level singletons or global mutable state.
- Parse config once at each entrypoint via `pydantic-settings`, pass it down
  explicitly; never read env vars, Kubernetes Secrets, or files from domain or
  adapter code.

## Idiomatic Python

- Full type hints everywhere; `mypy --strict` (or `pyright` strict mode)
  clean before merge. No untyped `Any` without a comment explaining why.
- Interfaces are `typing.Protocol`s, 1–3 methods, defined at the consumer —
  the Python equivalent of "accept interfaces, return structs."
- Validate every boundary (HTTP request/response, job payloads, LLM Gateway
  responses, DB rows) with pydantic models — never trust a raw `dict` past
  the adapter that received it.
- Specific exception types per domain concern; never a bare `except:` or
  `except Exception: pass`. Re-raise with `raise ... from err` to preserve
  the chain — the Python equivalent of wrapping with `%w`. Never swallow or
  log-and-continue silently.
- I/O is async (`asyncio`, `httpx.AsyncClient`, `asyncpg`/async SQLAlchemy,
  `asyncio.create_subprocess_exec` for analyzer runs) — this is a
  long-running, concurrent service, not a script; every I/O call takes a
  timeout and respects cancellation.
- Minimal public surface: leading-underscore for internals, `__all__` where a
  package's public API isn't obvious from its module layout.
- `ruff` (lint + format) and the strict type checker clean before merge —
  the Python equivalent of `gofmt`/`go vet`/`golangci-lint`.

## Simplicity

- No interface, abstraction, or generic without a second real implementation
  already in view — YAGNI over speculative flexibility.
- No separate message broker, no fourth deployable component beyond the
  three named above until a real requirement forces one — see the C4
  doc's "three deployable components, not five" note. The Admin UI
  (`apps/admin_api`) is the one exception already forced: an enterprise
  standalone-deployment requirement, per
  [ADR-0001](../adr/0001-self-hosted-kubernetes-deployment.md)'s
  2026-09-20 amendment.
- Explicit over clever.

---

**Verification tag:** `WKPYNQ`
