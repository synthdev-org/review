# syntax=docker/dockerfile:1
#
# Multi-stage, uv-managed image shared by both deployable entrypoints
# (apps/webhook_api, apps/worker) — per docs/rules/architecture.md, the two
# entrypoints are thin composition roots over one shared package tree, not
# separate codebases, so they share one image built from one Dockerfile.
# Phase 5's Docker Compose stack selects the entrypoint per service via
# `command:`; this same `runtime` stage is what Kubernetes/Helm run in
# production (ADR-0001).

ARG PYTHON_VERSION=3.12-slim

FROM python:${PYTHON_VERSION} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/
WORKDIR /app

# --- deps: production dependencies only, cached independently of app code ---
FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

# --- dev-deps: adds the dev dependency group (ruff, mypy, pytest, ...) ---
FROM deps AS dev-deps
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project

# Gitleaks — vendored, not a PyPI dependency (a Go binary with no
# first-party Python packaging), pinned to an explicit tag, per the plan's
# Assumptions section ("Analyzer binary packaging"). Used in both the
# `runtime` stage (adapter/analyzers/gitleaks.py, Phase 5) and the `dev`
# stage (so `pytest -m integration` run via `docker build --target dev`
# locally has the same binary CI's integration-test job installs separately
# on the bare runner — see .github/workflows/ci.yml).
FROM zricethezav/gitleaks:v8.21.2 AS gitleaks

# --- dev: full source + dev deps, used to run lint/type-check/test locally
# and in CI — `docker build --target dev` then `docker run <img> uv run ruff
# check .` etc. Never pushed/deployed.
FROM dev-deps AS dev
COPY --from=gitleaks /usr/bin/gitleaks /usr/local/bin/gitleaks
COPY . .
ENV PATH="/app/.venv/bin:${PATH}"

# --- runtime: production dependencies + app code only, no dev tooling ---
FROM base AS runtime
COPY --from=deps /app/.venv /app/.venv
COPY --from=gitleaks /usr/bin/gitleaks /usr/local/bin/gitleaks
ENV PATH="/app/.venv/bin:${PATH}"
COPY ingestion ./ingestion
COPY indexing ./indexing
COPY review ./review
COPY llmgateway ./llmgateway
COPY findings ./findings
COPY memory ./memory
COPY tenancy ./tenancy
COPY adapter ./adapter
COPY apps ./apps
COPY pyproject.toml alembic.ini ./

# Non-root container user (Phase 5) — this is the first phase running real
# subprocesses (Gitleaks, Semgrep) against untrusted (PR-controlled)
# content, per the Security review's "No process-level privilege
# separation" section: a compromised analyzer subprocess no longer runs as
# `root` inside the container. Created after every `COPY` above so file
# ownership is set (root-owned, read-only to `worker`) before the privilege
# drop; `--system --no-create-home` since this user never needs a login
# shell or home directory. Applies to both entrypoints this image serves
# (Webhook API and Worker) — a compromised dependency in either process
# gets the same reduced privileges, not just the Worker's own analyzer
# subprocesses.
RUN useradd --system --no-create-home worker
USER worker

# Default command runs the Webhook API; Compose/Helm override `command:` to
# run the Worker instead (`python -m apps.worker.main`) from this same image.
EXPOSE 8000
CMD ["uvicorn", "apps.webhook_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
