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

# --- embedding-model: download and checksum-verify the bundled local
# embedding model weights (US4, adapter/embedding/local_onnx.py) at build
# time, never at runtime — per the indexing delivery plan's Assumption 4
# ("model weights are baked into the Worker's container image at build
# time ... pinned checksum, not a mutable model-name reference alone"). A
# compromised/tampered model artifact would otherwise run in-process
# against every customer's proprietary source undetected (found during
# that plan's own security review); `sha256sum -c` below genuinely fails
# this stage — and therefore the whole image build — on any mismatch, not
# just a naming convention.
#
# Sourced from the exact Hugging Face repo/commit
# adapter/embedding/local_onnx.py's own module docstring names (verified
# directly against `fastembed`'s model registry before implementation, not
# assumed) — pinned to a commit SHA, not a mutable branch ref, so "the
# pinned checksum" and "the pinned source" are the same commitment, not two
# independent ones that could silently drift apart.
FROM debian:bookworm-slim AS embedding-model
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /model
COPY adapter/embedding/model.sha256 ./model.sha256
RUN set -eu; \
    repo="Qdrant/all-MiniLM-L6-v2-onnx"; \
    revision="5f1b8cd78bc4fb444dd171e59b18f3a3af89a079"; \
    for f in config.json model.onnx special_tokens_map.json tokenizer.json tokenizer_config.json vocab.txt; do \
        curl -fsSL -o "$f" "https://huggingface.co/${repo}/resolve/${revision}/${f}"; \
    done; \
    sha256sum -c model.sha256; \
    rm model.sha256

# --- frontend-build: apps/admin_api's own React + TypeScript + Vite
# frontend (Phase 8, Unit I, Assumption 9) — built here, in its own Node
# stage, so neither the `dev` nor `runtime` Python stage ever needs Node
# installed. Only this stage's own `dist/` output (a handful of static
# files) is copied into `runtime` below; `node_modules`/source never reach
# either Python stage. A fresh, dependency-only `npm ci` layer (cached
# independently of the frontend's own source, mirroring `deps`'s own
# `uv sync --no-install-project` split above) keeps a source-only change
# from invalidating the `npm ci` layer.
FROM node:20-slim AS frontend-build
WORKDIR /frontend
COPY apps/admin_api/frontend/package.json apps/admin_api/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/admin_api/frontend/ ./
RUN npm run build

# --- dev: full source + dev deps, used to run lint/type-check/test locally
# and in CI — `docker build --target dev` then `docker run <img> uv run ruff
# check .` etc. Never pushed/deployed. Gets the same checksum-verified
# model weights the `runtime` stage does (copied from the same
# `embedding-model` stage, not re-downloaded) — Phase 4's own integration
# tests (`adapter/embedding/test_local_onnx.py`,
# `adapter/embedding/test_startup_check.py`) run the real bundled model,
# not a mock, per `testing.md`'s adapter convention, and this is what makes
# that possible without network access from inside the `dev` container.
FROM dev-deps AS dev
COPY --from=gitleaks /usr/bin/gitleaks /usr/local/bin/gitleaks
COPY --from=embedding-model /model /app/models/all-MiniLM-L6-v2
COPY . .
ENV PATH="/app/.venv/bin:${PATH}"

# --- runtime: production dependencies + app code only, no dev tooling ---
FROM base AS runtime
COPY --from=deps /app/.venv /app/.venv
COPY --from=gitleaks /usr/bin/gitleaks /usr/local/bin/gitleaks
COPY --from=embedding-model /model /app/models/all-MiniLM-L6-v2
ENV PATH="/app/.venv/bin:${PATH}"
COPY ingestion ./ingestion
COPY indexing ./indexing
COPY review ./review
COPY llmgateway ./llmgateway
COPY findings ./findings
COPY memory ./memory
COPY tenancy ./tenancy
COPY iam ./iam
COPY adapter ./adapter
COPY apps ./apps
COPY pyproject.toml alembic.ini ./

# apps/admin_api's built frontend (Phase 8, Unit I, Assumption 9) —
# copied in after `COPY apps ./apps` so this overlay wins; `main.py`'s
# `create_frontend_router` serves it via a catch-all route excluding
# /api/* and /healthz.
COPY --from=frontend-build /frontend/dist ./apps/admin_api/frontend/dist

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
