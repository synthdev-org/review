# review

review is a self-hosted AI code review platform. You run it in your own infrastructure. It reviews GitHub pull requests and posts inline comments and a check-run status. Your code and your review history never leave your control.

[![CI](https://github.com/synthdev-org/review/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/synthdev-org/review/actions/workflows/ci.yml)
[![Quality gate](https://sonarcloud.io/api/project_badges/measure?project=synthdev-org_review&metric=alert_status)](https://sonarcloud.io/summary/overall?id=synthdev-org_review)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=synthdev-org_review&metric=coverage)](https://sonarcloud.io/summary/overall?id=synthdev-org_review)
[![Codacy grade](https://app.codacy.com/project/badge/Grade/5f1ef6a05d7d46ac92361bab77560236)](https://app.codacy.com/gh/synthdev-org/review/dashboard)
[![Codacy coverage](https://app.codacy.com/project/badge/Coverage/5f1ef6a05d7d46ac92361bab77560236)](https://app.codacy.com/gh/synthdev-org/review/dashboard)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

## Contents

- [What review does](#what-review-does)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Connect a GitHub App](#connect-a-github-app)
- [Configuration](#configuration)
- [Admin UI and sign-in](#admin-ui-and-sign-in)
- [Production deployment](#production-deployment)
- [Observability](#observability)
- [Run a review from the command line](#run-a-review-from-the-command-line)
- [Development and testing](#development-and-testing)
- [Project status](#project-status)
- [Security](#security)
- [Contributing](#contributing)
- [Documentation](#documentation)
- [License](#license)

## What review does

Hosted review tools send your diffs and source code to another company. review does not. You deploy it as container images on Kubernetes or with Docker Compose. It talks only to the GitHub organization and the LLM endpoint that you configure.

The design comes from research into how the leading commercial tools review code. The research is in [`docs/research/research-current-art.md`](docs/research/research-current-art.md). review uses these techniques:

- Indexing and retrieval: review splits your code into chunks that follow the syntax tree. It stores embeddings and a call and import graph in Postgres. It updates them on every push. A review comment can use the code around a change, not only the changed lines.
- Static analysis first: linters, secret scanning, and other analyzers run before any LLM call. Their output is a fixed reference that LLM findings are checked against.
- Separate verification: a second, independent LLM call checks each finding against a fixed rubric. The model never rates its own output.
- Learning with human approval: review records dismissed findings, accepted fixes, and developer reactions. A person must approve each learned item in the Admin UI before it changes future reviews.
- Any LLM endpoint: review does not proxy inference and does not hold provider credentials. You point it at any endpoint that uses an OpenAI-compatible or Anthropic-compatible API. Examples are hosted APIs, vLLM, and a local model server.
- Optional product knowledge: you can connect a separate product, knowhow, over MCP. It adds context from tools such as Confluence, Slack, and ticket systems. review does not need it. Everything else works the same without it.

## How it works

```
GitHub PR event --> Webhook API --> Job queue (Postgres) --> Worker
                                                               |
                  +--------------------------------------------+
                  | Indexing job             Review job         |
                  | AST chunking             Pre-filter          |
                  | embeddings               Static analysis     |
                  | call/import graph        Context builder     |
                  | -> Postgres/pgvector     LLM gateway         |
                  |                          Verify and merge    |
                  +--------------------------------------------+
                                                               |
                                                               v
                                       GitHub adapter posts inline PR
                                       comments and the check-run status
```

review has three programs that run from the same container image:

| Program | Path | Role |
|---|---|---|
| Webhook API | `apps/webhook_api` | Receives GitHub webhooks. It checks the signature, finds the organization and repository, and puts a job in the queue. |
| Worker | `apps/worker` | Takes jobs from the queue. It runs indexing jobs and review jobs. You can run many Workers. |
| Admin API and UI | `apps/admin_api` | Serves the Admin UI and its API. Admins use it to manage users, review settings, model routing, and learned items. |

All three programs use one Postgres database with the pgvector extension. The database holds the code index, findings history, learned items, and the job queue. review does not use a separate message broker. Every LLM call goes through the LLM gateway in `llmgateway/`, which uses [LiteLLM](https://github.com/BerriAI/litellm). The gateway sends each call to the endpoint that you set for the organization, the team, and the pipeline stage.

These directories hold the rest of the code:

| Path | Contents |
|---|---|
| `indexing/` | Syntax-aware chunking, embeddings, and call and import graph extraction. |
| `review/` | The review pipeline stages. |
| `llmgateway/` | LLM routing. |
| `findings/`, `memory/`, `tenancy/`, `ingestion/`, `iam/` | Domain code for findings, learned items, organization scoping, webhook input, and admin sign-in. |
| `adapter/` | All code that talks to the outside world: GitHub, analyzers, embeddings, storage, telemetry, and sign-in providers. |
| `charts/review/` | The Helm chart. |
| `observability/` | Grafana and OpenTelemetry configuration for the local stack. |

The rules for this layout are in [`docs/rules/architecture.md`](docs/rules/architecture.md). The architecture diagrams are in [`docs/reference/c4-architecture.md`](docs/reference/c4-architecture.md).

## Requirements

To run the stack with Docker Compose, you need:

- Docker with the Compose plugin.
- A GitHub account that can create a GitHub App for your organization.
- An LLM endpoint. The default configuration expects an OpenAI-compatible server at `host.docker.internal:1234`, for example [LM Studio](https://lmstudio.ai/). You can set another endpoint in `.env`.

To run in production, you also need:

- A Kubernetes cluster and Helm.
- Postgres with the pgvector extension. The Helm chart can install a bundled Postgres for trials.
- A TLS-terminating Ingress. The Admin UI session cookie is secure by default.

To develop review, you also need Python 3.12 (exactly 3.12), [uv](https://docs.astral.sh/uv/), and Node.js 20 for the Admin UI.

## Quick start

1. Copy `.env.example` to `.env`.
2. Replace every placeholder in `.env`. These values have no safe default. Each program does not start without its values:
   - Admin API: `REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_USERNAME`, `REVIEW_ADMIN_API_ADMIN_BOOTSTRAP_PASSWORD`, and `REVIEW_ADMIN_API_ADMIN_CSRF_SECRET`. The password must have at least 12 characters.
   - Webhook API: `REVIEW_WEBHOOK_API_GITHUB_WEBHOOK_SECRET`.
3. Start your LLM server, or set `REVIEW_WORKER_LLM_ENDPOINT`, `REVIEW_WORKER_LLM_API_KEY`, and `REVIEW_WORKER_LLM_MODEL` in `.env`.
4. Start the stack:

   ```sh
   docker compose up
   ```

   Compose starts Postgres and applies the database migrations. Then it starts the Webhook API, the Admin API, and the Worker. The command stays in the foreground until you stop it.

5. Make sure that the Webhook API is healthy:

   ```sh
   curl http://localhost:8000/healthz
   ```

6. Open the Admin UI at `http://localhost:8001`. Sign in with the bootstrap username and password from `.env`.

`.env` is in `.gitignore`. Do not commit it.

## Connect a GitHub App

review reads pull requests as a GitHub App. The Webhook API can create the App for you with the GitHub App manifest flow.

1. In `.env`, set `REVIEW_WEBHOOK_API_SETUP_MODE=true` and set `REVIEW_WEBHOOK_API_SETUP_STATE_SECRET` to a long random value.
2. Restart the Webhook API. It must be reachable from your browser. GitHub must also be able to reach it for webhook delivery.
3. Open `/setup/start` on the Webhook API in your browser. Follow the GitHub prompts.
4. GitHub returns the App ID and the private key in the callback response. Copy them into `.env`: `REVIEW_WEBHOOK_API_GITHUB_APP_ID`, `REVIEW_WEBHOOK_API_GITHUB_APP_PRIVATE_KEY`, `REVIEW_WORKER_GITHUB_APP_ID`, and `REVIEW_WORKER_GITHUB_APP_PRIVATE_KEY`.
5. Set `REVIEW_WEBHOOK_API_SETUP_MODE=false` and restart the stack.
6. Install the App on the repositories that you want reviewed. GitHub sends events to `POST /webhooks/github` on the Webhook API.

Keep setup mode off after the setup. It exposes the setup routes.

## Configuration

review reads all settings from environment variables. Each program has its own prefix:

| Prefix | Program |
|---|---|
| `REVIEW_WEBHOOK_API_` | Webhook API |
| `REVIEW_WORKER_` | Worker |
| `REVIEW_ADMIN_API_` | Admin API |
| `REVIEW_DATABASE_URL` | All programs. This is the Postgres connection string. |

[`.env.example`](.env.example) lists every setting with a default or a placeholder, and it groups the settings by program. These are the settings that you will change most often:

| Setting | Purpose |
|---|---|
| `REVIEW_WORKER_LLM_ENDPOINT`, `REVIEW_WORKER_LLM_API_KEY`, `REVIEW_WORKER_LLM_MODEL` | The default LLM endpoint, key, and model. You can route individual pipeline stages to other models in the Admin UI. |
| `REVIEW_WEBHOOK_API_INDEXING_ENABLED` | Turns on repository indexing. It is off by default. |
| `REVIEW_WORKER_DRY_RUN` | When true, the Worker runs the pipeline and does not post to GitHub. |
| `REVIEW_WEBHOOK_API_SECRET_REDACTION_ENABLED` and `REVIEW_WORKER_SECRET_REDACTION_ENABLED` | Remove secrets from text before review stores it or sends it to an LLM. Both are on by default. |
| `REVIEW_ADMIN_API_SESSION_COOKIE_SECURE` | Must be true when the Admin UI is served over HTTPS. The Compose file sets it to false for local use. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Where review sends OpenTelemetry metrics and traces. |

Product knowledge, model routing, and the classifier are optional features. You set the endpoint and the credential reference for each one in the Admin UI. The operator rules for these features are in [`docs/reference/operations.md`](docs/reference/operations.md).

## Admin UI and sign-in

The Admin UI shows pipeline statistics, and it lets admins manage:

- Users and their roles.
- Settings for each organization, repository, and team.
- Model routing for each pipeline stage.
- The learned items that wait for approval, and the optional product knowledge and classifier providers.

There are two roles. An admin can change anything. A viewer can read everything and cannot change anything. review enforces the roles on the server.

### Local sign-in

Local sign-in always works. It needs no license and no outside service. On first start, review creates one admin user from the bootstrap username and password in `.env`. After that, an admin creates other users in the Admin UI. Login attempts are rate limited for each username.

### OIDC sign-in

You can add single sign-on with any OpenID Connect provider. OIDC is an extra way to sign in. It does not replace local sign-in. OIDC is off by default.

review uses the authorization code flow with PKCE. It checks the `state`, the `nonce`, the token signature, the issuer, and the audience. It accepts only RS256 and ES256 signatures.

review does not create users at sign-in. An admin must first create a local user. The user name must equal the value of the `preferred_username` claim in the ID token. If no active local user matches, sign-in fails and the Admin UI shows a general error.

Treat your identity provider as trusted for user names. A person who can set their `preferred_username` to the name of a local user can sign in as that user.

#### Authentik

1. In Authentik, create an OAuth2/OpenID provider and an application that uses it. Note the application slug.
2. Set the redirect URI of the provider to `https://<your-admin-host>/api/v1/auth/oidc/callback`.
3. In `.env`, set these values:

   ```sh
   REVIEW_ADMIN_API_OIDC_PROVIDER=authentik
   REVIEW_ADMIN_API_OIDC_CLIENT_ID=<client id from Authentik>
   REVIEW_ADMIN_API_OIDC_CLIENT_SECRET=<client secret from Authentik>
   REVIEW_ADMIN_API_OIDC_REDIRECT_URI=https://<your-admin-host>/api/v1/auth/oidc/callback
   REVIEW_ADMIN_API_OIDC_AUTHENTIK_BASE_URL=https://authentik.example.com
   REVIEW_ADMIN_API_OIDC_AUTHENTIK_APP_SLUG=<application slug>
   ```

   For a public client that uses PKCE only, leave `REVIEW_ADMIN_API_OIDC_CLIENT_SECRET` unset.

4. Restart the Admin API. The sign-in page now shows a "Sign in with SSO" link.

review builds the issuer URL as `<base url>/application/o/<slug>/`.

#### Other OIDC providers

Set `REVIEW_ADMIN_API_OIDC_PROVIDER=generic` and set `REVIEW_ADMIN_API_OIDC_ISSUER` to the issuer URL. review reads the provider configuration from `<issuer>/.well-known/openid-configuration`. The client ID, client secret, and redirect URI settings are the same as for Authentik. Two more settings are optional: `REVIEW_ADMIN_API_OIDC_SCOPES` (default `openid profile email`) and `REVIEW_ADMIN_API_OIDC_USERNAME_CLAIM` (default `preferred_username`).

To add a new provider type, write a class that follows the `OIDCProvider` protocol in [`iam/oidc.py`](iam/oidc.py). See [`adapter/auth/oidc.py`](adapter/auth/oidc.py) for the two existing classes. The decision record is [`docs/adr/0010-oidc-admin-sign-in-and-fixed-role-split.md`](docs/adr/0010-oidc-admin-sign-in-and-fixed-role-split.md).

## Production deployment

Production runs on Kubernetes with the Helm chart in [`charts/review/`](charts/review/). The chart creates separate Deployments and Services for the Webhook API, the Worker, and the Admin API, and an Ingress that routes to the Webhook API and the Admin API.

Set the secrets in the chart values, or point them at Kubernetes Secrets that you manage. Secret values include the database URL, the Admin API bootstrap credentials, the CSRF secret, and the OIDC client secret. Set `adminApi.oidc.*` to turn on OIDC.

Run the database migrations before you run `helm upgrade`. [`docs/reference/operations.md`](docs/reference/operations.md) explains how, and it also covers rate limits, credential rotation, and fixes for common start-up failures.

The chart is before version 1.0. Its `values.yaml` can change without notice. Do not build automation on its shape yet.

## Observability

review exports OpenTelemetry metrics and traces. Set `OTEL_EXPORTER_OTLP_ENDPOINT` to send them to your own collector. For a local demo, start the bundled collector, Prometheus, and Grafana:

```sh
docker compose --profile observability up
```

Grafana runs on port 3000. The bundled Grafana account is for local demos only. Do not use it in production. The Admin UI dashboard reads its statistics from Prometheus, which you set with `REVIEW_ADMIN_API_PROMETHEUS_BASE_URL`.

## Run a review from the command line

You can run the review pipeline on a local git repository, with no GitHub App and no database:

```sh
python -m apps.local_review --base main --head HEAD --repo .
```

The runner reads its settings from `.env.local` or from `REVIEW_LOCAL_*` environment variables. It runs the pre-filter, review generation, verification, and output stages. It does not run static analysis, the context builder, the intent summary, or the spec review.

## Development and testing

Install the dependencies:

```sh
uv sync --locked
```

The tests follow a pyramid. Unit tests cover the domain packages and do no I/O. Integration tests cover one adapter each against a real Postgres from `testcontainers-python`, or against recorded GitHub and LLM responses. End-to-end tests start the Docker Compose stack and use HTTP. [`docs/rules/testing.md`](docs/rules/testing.md) has the rules, including how a user story maps to an integration test.

```sh
uv run pytest                    # unit tests
uv run pytest -m integration     # integration tests, needs Docker
```

The Admin API fails at start-up if the local embedding model is missing. The Docker image includes the model. To run the tests on your host, run `scripts/fetch-embedding-model.sh` once. The default path is `/app/models/all-MiniLM-L6-v2`. You can pass another path as the first argument and set `REVIEW_ADMIN_API_EMBEDDING_MODEL_PATH` and `REVIEW_WORKER_EMBEDDING_MODEL_PATH` to match.

Before you open a pull request, make sure that these checks pass:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict .
```

For the Admin UI in `apps/admin_api/frontend`:

```sh
npm ci
npm run typecheck
npm test
```

## Project status

review is under active development. The review pipeline works from start to finish. It covers webhook intake, incremental indexing, static analysis, context building, review generation, verification, and posting to GitHub. The Admin UI, learned items (Memory), multi-organization settings, optional product knowledge, and OpenTelemetry observability are in place. The version is 0.1.0.

The Helm chart is before version 1.0. The roadmap is in [`docs/roadmap.md`](docs/roadmap.md).

### Releases

The Helm chart and the container image use the image `ghcr.io/synthdev-org/review`. The "Publish image" workflow builds and publishes the image when a version tag such as `v0.1.0` is pushed. The project is before 1.0, so expect changes between versions. [`CHANGELOG.md`](CHANGELOG.md) lists them.

## Security

review keeps your code inside your infrastructure. The Worker sends diffs and context only to the LLM endpoint that you configure. Secret redaction removes secrets from text before storage and before LLM calls. The optional product knowledge connection works only for private repositories. It uses a network guard that blocks unapproved destinations.

To report a security problem, follow [`SECURITY.md`](SECURITY.md). Do not open a public issue for a vulnerability.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md). In short: before you change code, read [`AGENTS.md`](AGENTS.md), [`docs/rules/architecture.md`](docs/rules/architecture.md), and [`docs/rules/testing.md`](docs/rules/testing.md). These rules are mandatory. Follow them as written.

1. Open an issue that describes the change, or comment on an existing issue.
2. Make your change on a branch. Add tests at the correct level of the pyramid.
3. Run the checks from the [Development and testing](#development-and-testing) section.
4. Open a pull request.

A change that breaks an architecture rule needs a new decision record in [`docs/adr/`](docs/adr/).

## Documentation

| Document | Contents |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Project overview and required reading. |
| [`docs/reference/c4-architecture.md`](docs/reference/c4-architecture.md) | System architecture with diagrams. |
| [`docs/reference/operations.md`](docs/reference/operations.md) | Operator runbook. |
| [`docs/roadmap.md`](docs/roadmap.md) | Delivery phases. |
| [`CHANGELOG.md`](CHANGELOG.md) | Changes in each version. |
| [`docs/documentation_index.md`](docs/documentation_index.md) | Index of every document in `docs/`. |
| [`docs/adr/`](docs/adr/) | Architecture decision records. |
| [`docs/specs/`](docs/specs/) | Product specifications with user stories. |
| [`docs/rules/`](docs/rules/) | Mandatory engineering rules. |
| [`docs/research/research-current-art.md`](docs/research/research-current-art.md) | The research that the design comes from. |

## License

review is licensed under the [MIT License](LICENSE).
