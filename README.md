# review

Self-hosted, enterprise AI code review platform. See [`AGENTS.md`](AGENTS.md)
for the project overview and [`docs/roadmap.md`](docs/roadmap.md) /
[`docs/reference/c4-architecture.md`](docs/reference/c4-architecture.md) for
the system's architecture and delivery sequencing.

## Local setup

The whole stack (Webhook API + Worker + Postgres/pgvector) runs via Docker
Compose, using the same images Kubernetes runs in production.

1. Copy `.env.example` to `.env` and replace every placeholder with a real
   value — `.env` is gitignored, so this never risks committing a secret.
2. Optionally start [LM Studio](https://lmstudio.ai/) locally with a model
   loaded and its OpenAI-compatible server running on port `1234` — the
   Worker's LLM Gateway points at `host.docker.internal:1234` by default.
3. `docker compose up` — this blocks until Postgres is healthy, migrations
   (including the pgvector-presence check) have applied, and the Webhook
   API's `/healthz` returns 200.

## Helm chart

[`charts/review/`](charts/review/) is **pre-1.0 / unstable** — its
`values.yaml` shape may still change before the roadmap's "Production
hardening" phase. Don't build automation against it expecting a stable
contract yet; see the header comments in `charts/review/Chart.yaml` and
`charts/review/values.yaml` for details.
