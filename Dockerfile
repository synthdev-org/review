# syntax=docker/dockerfile:1
#
# Multi-stage, uv-managed image shared by all three deployable programs
# (apps/webhook_api, apps/worker, apps/admin_api) — per
# docs/rules/architecture.md, they are thin composition roots over one
# shared package tree, not separate codebases, so they share one image
# built from one Dockerfile. The Docker Compose stack selects the program
# per service via `command:`; this same `runtime` stage is what
# Kubernetes/Helm run in production (ADR-0001).

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
    uv sync --locked --no-install-project --no-dev --no-build

# --- dev-deps: adds the dev dependency group (ruff, mypy, pytest, ...) ---
FROM deps AS dev-deps
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-build

# --- semgrep-tool: Semgrep in its own locked environment (tools/semgrep).
# Semgrep hard-pins mcp 1.x, which conflicts with the main project's
# `mcp>=2.3,<3` (ADR-0013), so it is not in the main dependency group; the
# adapter only ever runs it as a `semgrep` subprocess found on PATH.
FROM base AS semgrep-tool
COPY tools/semgrep/pyproject.toml tools/semgrep/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_PROJECT_ENVIRONMENT=/opt/semgrep uv sync --locked --no-install-project --no-build

# Gitleaks — vendored, not a PyPI dependency (a Go binary with no
# first-party Python packaging), pinned to an explicit tag. Used in both the
# `runtime` stage (adapter/analyzers/gitleaks.py) and the `dev`
# stage (so `pytest -m integration` run via `docker build --target dev`
# locally has the same binary CI's integration-test job installs separately
# on the bare runner — see .github/workflows/ci.yml).
FROM zricethezav/gitleaks:v8.21.2 AS gitleaks

# --- embedding-model: download and checksum-verify the bundled local
# embedding model weights (US4, adapter/embedding/local_onnx.py) at build
# time, never at runtime, against a pinned checksum rather than a mutable
# model-name reference alone. A compromised/tampered model artifact would
# otherwise run in-process against every customer's proprietary source
# undetected; `sha256sum -c` below genuinely fails
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
        curl --proto "=https" -fsSL -o "$f" "https://huggingface.co/${repo}/resolve/${revision}/${f}"; \
    done; \
    sha256sum -c model.sha256; \
    rm model.sha256

# --- frontend-build: apps/admin_api's own React + TypeScript + Vite
# frontend — built here, in its own Node
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
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY apps/admin_api/frontend/ ./
RUN npm run build

# --- dev: full source + dev deps, used to run lint/type-check/test locally
# and in CI — `docker build --target dev` then `docker run <img> uv run ruff
# check .` etc. Never pushed/deployed. Gets the same checksum-verified
# model weights the `runtime` stage does (copied from the same
# `embedding-model` stage, not re-downloaded) — the embedding adapter's own
# integration tests (`adapter/embedding/test_local_onnx.py`,
# `adapter/embedding/test_startup_check.py`) run the real bundled model,
# not a mock, per `testing.md`'s adapter convention, and this is what makes
# that possible without network access from inside the `dev` container.
FROM dev-deps AS dev
COPY --from=gitleaks /usr/bin/gitleaks /usr/local/bin/gitleaks
COPY --from=semgrep-tool /opt/semgrep /opt/semgrep
RUN ln -s /opt/semgrep/bin/semgrep /usr/local/bin/semgrep
COPY --from=embedding-model /model /app/models/all-MiniLM-L6-v2
COPY . .
ENV PATH="/app/.venv/bin:${PATH}"

# --- runtime: production dependencies + app code only, no dev tooling ---
FROM base AS runtime
COPY --from=deps /app/.venv /app/.venv
COPY --from=gitleaks /usr/bin/gitleaks /usr/local/bin/gitleaks
COPY --from=semgrep-tool /opt/semgrep /opt/semgrep
RUN ln -s /opt/semgrep/bin/semgrep /usr/local/bin/semgrep
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
# The fake MCP provider (fault injection) is test tooling: never in the
# production image.
RUN rm -rf ./adapter/product_knowledge/testing
# Likewise the in-memory fake Classifier.
RUN rm -rf ./llmgateway/testing
COPY apps ./apps
# The Admin API test harnesses are test doubles too.
RUN rm -rf ./apps/admin_api/testing
COPY pyproject.toml alembic.ini ./

# apps/admin_api's built frontend —
# copied in after `COPY apps ./apps` so this overlay wins; `main.py`'s
# `create_frontend_router` serves it via a catch-all route excluding
# /api/* and /healthz.
COPY --from=frontend-build /frontend/dist ./apps/admin_api/frontend/dist

# Non-root container user — the image runs real subprocesses (Gitleaks,
# Semgrep) against untrusted (PR-controlled) content, so a compromised
# analyzer subprocess does not run as `root` inside the container. Created
# after every `COPY` above so file
# ownership is set (root-owned, read-only to `worker`) before the privilege
# drop; `--system --no-create-home` since this user never needs a login
# shell or home directory. Applies to every program this image serves
# (Webhook API, Worker, Admin API) — a compromised dependency in any
# process gets the same reduced privileges, not just the Worker's own
# analyzer subprocesses.
RUN useradd --system --no-create-home worker
# Semgrep writes a log and a settings file under $HOME at start-up, and the
# `worker` user has no home directory (it exits with PermissionError on
# /home/worker otherwise) — point both at /tmp.
ENV SEMGREP_LOG_FILE=/tmp/semgrep.log \
    SEMGREP_SETTINGS_FILE=/tmp/semgrep-settings.yml
USER worker

# Default command runs the Webhook API; Compose/Helm override `command:` to
# run the Worker instead (`python -m apps.worker.main`) from this same image.
EXPOSE 8000
CMD ["uvicorn", "apps.webhook_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
