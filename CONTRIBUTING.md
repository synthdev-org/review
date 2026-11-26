# Contributing to review

Thank you for helping to improve review. This document explains how to set up your machine, which rules your change must follow, and how to open a pull request.

By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE). You also agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

To report a security problem, do not use an issue or a pull request. Follow [SECURITY.md](SECURITY.md).

## Before you start

1. Read [`AGENTS.md`](AGENTS.md). It describes the project and lists the required reading.
2. Read [`docs/rules/architecture.md`](docs/rules/architecture.md) in full.
3. Read [`docs/rules/testing.md`](docs/rules/testing.md) in full.
4. Open an issue that describes your change, or comment on an existing issue. For a large change, wait for a maintainer to reply before you write code.

The two rule files are mandatory. A pull request that breaks them cannot be merged.

## Set up your machine

You need Python 3.12 (exactly 3.12), [uv](https://docs.astral.sh/uv/), Docker, and Node.js 20. Docker runs the Postgres database for the integration tests. Node.js builds the Admin UI.

1. Install the Python dependencies:

   ```sh
   uv sync --locked
   ```

2. Download the local embedding model. The Admin API and the Worker stop at start-up without it, and some tests start them:

   ```sh
   scripts/fetch-embedding-model.sh
   ```

   The script saves the model to `/app/models/all-MiniLM-L6-v2`. You can pass another directory as the first argument. If you do, set `REVIEW_ADMIN_API_EMBEDDING_MODEL_PATH` and `REVIEW_WORKER_EMBEDDING_MODEL_PATH` to the same directory.

3. Install the Admin UI dependencies:

   ```sh
   cd apps/admin_api/frontend
   npm ci
   ```

4. To run the whole stack, copy `.env.example` to `.env` and replace every placeholder. Then run `docker compose up`. The [README](README.md) has the details.

Never commit `.env` or any real secret. `.env.example` holds placeholder values only.

## Follow the architecture rules

[`docs/rules/architecture.md`](docs/rules/architecture.md) is the full text. Reviewers look for these rules most often:

- Group code by bounded context (for example `review`, `findings`, `tenancy`), not by layer.
- Domain packages must not import infrastructure libraries such as `fastapi`, `sqlalchemy`, or `httpx`. Only adapters import them.
- Define an interface as a `typing.Protocol` in the code that uses it. Do not add an interface until it has a second real implementation.
- Create concrete objects only in the entrypoints (`apps/webhook_api`, `apps/worker`, `apps/admin_api`). Pass them in through constructors. Do not use module-level singletons.
- Read configuration once in each entrypoint with `pydantic-settings`. Domain code and adapter code must not read environment variables or files.
- Use type hints everywhere. The code must pass `mypy --strict`.
- Validate every input boundary with a pydantic model.
- Make all I/O asynchronous and give each call a timeout.
- Catch specific exceptions. Never use a bare `except:`. Never swallow an error.

If your change needs to break a rule, write a new decision record in [`docs/adr/`](docs/adr/) first. A one-off exception is not allowed.

## Write tests

[`docs/rules/testing.md`](docs/rules/testing.md) is the full text. The test suite has three levels:

- Unit tests cover one domain package. They use fakes and do no I/O. Put `test_foo.py` next to `foo.py`.
- Integration tests cover one adapter against a real system. Database tests use a real Postgres from `testcontainers-python`. Mark each integration test with `@pytest.mark.integration`.
- End-to-end tests run the Docker Compose stack over HTTP. They live in `tests/e2e/`.

Use a fake instead of a mock for an adapter. Tests must not call a live LLM by default.

If your change adds or changes a user story in a specification, the story needs at least one integration test. The specification must list that test. A user story is written as "As a, I want, so that", with Given, When, Then criteria.

Run the tests:

```sh
uv run pytest                    # unit tests
uv run pytest -m integration     # integration tests, needs Docker
```

For the Admin UI, run `npm test` in `apps/admin_api/frontend`.

## Run the checks

Before you open a pull request, make sure that all of these pass:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict .
```

For changes to the Admin UI, run these commands in `apps/admin_api/frontend`:

```sh
npm run typecheck
npm test
```

The same checks run in CI on every pull request. CI also runs Snyk (dependency vulnerabilities) and SonarCloud (code quality and coverage). It sends the coverage report to Codacy. Codacy analyzes each pull request and posts comments. Fix the issues that these tools report, or explain in the pull request why an issue does not apply.

## Write documentation

If your change affects behavior that a user or an operator can see, update the documentation in the same pull request. This includes the README, the files in `docs/`, and `.env.example`. Add every new setting to `.env.example` with a placeholder or a safe default.

## Commit and open a pull request

1. Create a branch from `main`.
2. Make small commits. Write the subject line in the imperative mood, such as "Add OIDC login". Keep it under 72 characters. Use the body to explain why you made the change.
3. Push your branch and open a pull request against `main`.
4. In the description, state what the change does and why. Link the issue. List the tests that you added.
5. Answer review comments with new commits. A maintainer will merge the pull request when the checks pass and the review is finished.

Keep each pull request to one topic. A pull request that mixes a refactor with a new feature is hard to review, and a maintainer can ask you to split it.

## Ask for help

If a rule is unclear, ask in the issue or in the pull request. A maintainer will answer there.
