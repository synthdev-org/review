# Testing Rules (Mandatory)

Companion to [`architecture.md`](architecture.md) — the pyramid follows that
document's package boundaries and pipeline stages, not generic advice.
Deviating requires a new ADR, not a one-off exception.

## The pyramid

- **Unit** (majority): one domain package (`ingestion`, `indexing`, `review`,
  `llmgateway`, `findings`, `memory`, `tenancy`) in isolation. Fakes at
  consumer-defined `typing.Protocol` boundaries (per `architecture.md`). No
  real I/O, adapters, network, or database.
- **Integration** (middle): one real adapter (`adapter/github`,
  `adapter/analyzers`, `adapter/storage`, `adapter/llm`) against a real or
  high-fidelity recorded external system — the static-analyzer adapter
  against actual binaries on fixtures, the storage adapter against a real
  Postgres/pgvector (via `testcontainers-python`), the GitHub adapter against
  a recorded cassette (`respx` for `httpx`) or a throwaway test-org repo, the
  LLM gateway against a recorded LiteLLM cassette or a local LM Studio
  instance. Proves the code talks to the outside world correctly.
- **E2E** (few): the built container images, run via the same Docker Compose
  stack as local development (Webhook API + Worker + Postgres), driven by a
  synthetic GitHub webhook payload against a stubbed GitHub endpoint —
  asserts posted-comment shape and check-run status. Happy path plus the
  failure modes that matter (analyzer crash, LLM timeout, no findings,
  malformed webhook signature).

## PRD/spec → user story → integration test

- Every PRD/spec states user stories as `As a / I want / so that` with
  `Given/When/Then` acceptance criteria — never prose requirements.
- Every user story maps to at least one **integration test**, not a unit
  test — a story is inherently a boundary crossing (webhook → pipeline →
  adapter → external system), and only an integration test proves it
  satisfied.
- The PRD/plan carries a traceability table (Story ID → G/W/T → integration
  test path). A phase isn't done until that test exists and passes.
- New domain logic a story touches still needs unit tests — necessary, not
  sufficient.

## Conventions

- Unit tests co-located: `foo.py` / `test_foo.py` in the same package.
  Integration tests co-located too, marked `@pytest.mark.integration` (fast
  default run stays `pytest`; the middle layer runs via
  `pytest -m integration`).
- E2E tests live in `tests/e2e/`, drive the Docker Compose stack over HTTP —
  no shortcuts that bypass the Webhook API's actual entrypoint.
- Test doubles: fakes over mocks for adapters — matches the small-Protocol
  rule, stays honest as the interface evolves.
- LLM determinism: integration tests use recorded response cassettes by
  default (LiteLLM's mocking support or a recorded-fixture wrapper). A
  live-mode flag exists for manual/nightly runs against a real endpoint
  only — never the default CI gate.
- Every DB-touching integration test runs against a real Postgres/pgvector
  via `testcontainers-python`, not sqlite-in-memory — pgvector's index
  behavior isn't something a substitute engine can stand in for.

## CI gating

- Unit: every push. Integration: every PR. E2E: on merge to main (or
  nightly).

---

**Verification tag:** `RQPTVE`
