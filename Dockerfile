# syntax=docker/dockerfile:1
#
# Multi-stage, uv-managed image shared by both deployable entrypoints
# (apps/webhook_api, apps/worker) — per docs/rules/architecture.md, the two
# entrypoints are thin composition roots over one shared package tree, not
# separate codebases, so they share one image built from one Dockerfile.
# Phase 5's Docker Compose stack selects the entrypoint per service via
# `command:`; this same `runtime` stage is what Kubernetes/Helm run in
# production (ADR-0001). The Worker alone runs the `worker-runtime` target
# (this `runtime` plus the static-analysis interpreters; see its stage below).

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

# --- semgrep-tool: Semgrep in its own locked environment (tools/semgrep).
# Semgrep hard-pins mcp 1.x, which conflicts with the main project's
# `mcp>=2.3,<3` (ADR-0013), so it is not in the main dependency group; the
# adapter only ever runs it as a `semgrep` subprocess found on PATH.
FROM base AS semgrep-tool
COPY tools/semgrep/pyproject.toml tools/semgrep/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_PROJECT_ENVIRONMENT=/opt/semgrep uv sync --locked --no-install-project

# Gitleaks — vendored, not a PyPI dependency (a Go binary with no
# first-party Python packaging), pinned to an explicit tag, per the plan's
# Assumptions section ("Analyzer binary packaging"). Used in both the
# `runtime` stage (adapter/analyzers/gitleaks.py, Phase 5) and the `dev`
# stage (so `pytest -m integration` run via `docker build --target dev`
# locally has the same binary CI's integration-test job installs separately
# on the bare runner — see .github/workflows/ci.yml).
FROM zricethezav/gitleaks:v8.21.2 AS gitleaks

# --- ruff: the pinned Ruff binary for adapter/analyzers/ruff.py (spec-static-
# analysis-expansion Unit C). Fetched at build time only (the engine itself is
# offline) and verified with `sha256sum -c` against a digest committed here,
# so a tampered or swapped release asset fails the whole image build. Keep the
# version in step with ci.yml's "Install Ruff (pinned, checksum-verified)" step.
FROM debian:bookworm-slim AS ruff
ARG TARGETARCH
ARG RUFF_VERSION=0.16.5
ARG RUFF_SHA256_AMD64=65b8bae7e43f12a91b71036a52176012b3aefb725d5ae263e2771474110a0983
ARG RUFF_SHA256_ARM64=796079ea998dba3e455394077ba51a4c500c2402d3920580c646a0580f20370c
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /ruff
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
        amd64) triple=x86_64-unknown-linux-gnu; sha="$RUFF_SHA256_AMD64" ;; \
        arm64) triple=aarch64-unknown-linux-gnu; sha="$RUFF_SHA256_ARM64" ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    archive="ruff-${triple}.tar.gz"; \
    curl -fsSL -o "$archive" "https://github.com/astral-sh/ruff/releases/download/${RUFF_VERSION}/${archive}"; \
    echo "${sha}  ${archive}" | sha256sum -c -; \
    tar xzf "$archive" --strip-components=1 "ruff-${triple}/ruff"; \
    ./ruff --version

# --- trivy: the pinned Trivy binary for adapter/analyzers/trivy.py (spec-static-
# analysis-expansion Unit F; `trivy config` only). One static Go binary with the
# misconfiguration checks embedded, so no check/DB download ever happens at run
# time. Verified with `sha256sum -c` against digests committed here (from the
# release's trivy_<version>_checksums.txt). Keep the version in step with ci.yml's
# "Install Trivy (pinned, checksum-verified)" step.
FROM debian:bookworm-slim AS trivy
ARG TARGETARCH
ARG TRIVY_VERSION=0.76.0
ARG TRIVY_SHA256_AMD64=e24b1c51293c1b15e4ba8d45bbc7891ba053f90278ec2698f3562c993e145fe7
ARG TRIVY_SHA256_ARM64=aab21e677a7b7071fc79f1711480840754c18a1b9d9cefa7ccd06af63a0e8f4c
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /trivy
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
        amd64) arch=64bit; sha="$TRIVY_SHA256_AMD64" ;; \
        arm64) arch=ARM64; sha="$TRIVY_SHA256_ARM64" ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    archive="trivy_${TRIVY_VERSION}_Linux-${arch}.tar.gz"; \
    curl -fsSL -o "$archive" "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/${archive}"; \
    echo "${sha}  ${archive}" | sha256sum -c -; \
    tar xzf "$archive" trivy; \
    ./trivy --version

# --- node-runtime: the pinned Node.js binary for ESLint (adapter/analyzers/
# eslint.py, plan-phase-9 Phase 4). Official tarball, verified with
# `sha256sum -c` against digests committed here (from the release's
# SHASUMS256.txt), so a swapped asset fails the image build. Only reaches the
# `dev` and worker-only `worker-runtime` targets, never `runtime` (the Webhook
# and Admin API images carry no interpreter). Keep NODE_VERSION in step with
# ci.yml's "Install ESLint" step.
FROM debian:bookworm-slim AS node-runtime
ARG TARGETARCH
ARG NODE_VERSION=22.23.3
ARG NODE_SHA256_AMD64=1084aa36196bba4c3a5e69a1ee388a6e4ff729dad09445fbcd434b28fe3c24af
ARG NODE_SHA256_ARM64=5ced2d48d1d7198739b7f86804de0171aefb6823b684b12341d3321afc3cb0b2
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /node-dl
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
        amd64) arch=x64; sha="$NODE_SHA256_AMD64" ;; \
        arm64) arch=arm64; sha="$NODE_SHA256_ARM64" ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    archive="node-v${NODE_VERSION}-linux-${arch}.tar.gz"; \
    curl -fsSL -o "$archive" "https://nodejs.org/dist/v${NODE_VERSION}/${archive}"; \
    echo "${sha}  ${archive}" | sha256sum -c -; \
    mkdir /node; \
    tar xzf "$archive" --strip-components=1 -C /node; \
    /node/bin/node --version

# --- eslint-tool: ESLint + its fixed plugin set (tools/eslint), installed from
# the committed package-lock.json, whose sha512 integrity fields make `npm ci`
# fail on any tampered or swapped package. `--ignore-scripts`: no package
# install script runs. This is the only place npm touches the network; the
# analyzer never installs or downloads anything at scan time.
FROM debian:bookworm-slim AS eslint-tool
COPY --from=node-runtime /node /usr/local
WORKDIR /opt/eslint
COPY tools/eslint/package.json tools/eslint/package-lock.json tools/eslint/review-eslint tools/eslint/formatter.cjs ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && rm -rf /root/.npm

# --- php-runtime: a minimal PHP CLI built from the official source tarball for
# PHPStan (adapter/analyzers/phpstan.py, plan-phase-9 Phase 7). Source, not a
# distro package, so the version is pinned and `sha256sum -c` verifies the
# tarball (digest from php.net's release feed). `--disable-all` leaves only the
# core extensions plus exactly what the phar needs (phar, tokenizer, mbstring,
# ctype, filter): no network wrappers' extensions (curl/openssl/sockets), no XML,
# no database drivers, no composer/pear. Built on the same base as `runtime` so
# the glibc matches; only reaches `dev` and the worker-only `worker-runtime`.
# Keep PHP_VERSION in step with ci.yml's "Install PHP + PHPStan" step.
FROM python:${PYTHON_VERSION} AS php-runtime
ARG PHP_VERSION=8.4.26
ARG PHP_SHA256=32a2de53862ad44ed4a5005244ce4f1b50c271e74dced215449a4443b40569f1
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils build-essential pkg-config libonig-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /php-src
RUN set -eu; \
    archive="php-${PHP_VERSION}.tar.xz"; \
    curl -fsSL -o "$archive" "https://www.php.net/distributions/${archive}"; \
    echo "${PHP_SHA256}  ${archive}" | sha256sum -c -; \
    tar xJf "$archive" --strip-components=1; \
    ./configure --prefix=/opt/php --disable-all --disable-cgi --disable-phpdbg \
        --without-pear --enable-cli --enable-phar --enable-tokenizer \
        --enable-mbstring --enable-ctype --enable-filter; \
    make -j"$(nproc)"; \
    make install-cli; \
    strip /opt/php/bin/php; \
    /opt/php/bin/php -n -m

# --- phpstan-tool: the pinned PHPStan phar (tools/phpstan). Verified with
# `sha256sum -c` against the digest committed here (it equals the `digest`
# GitHub shows for the release asset). A single self-contained phar: no composer,
# no autoloader, no network at run time. Keep PHPSTAN_VERSION in step with ci.yml.
FROM debian:bookworm-slim AS phpstan-tool
ARG PHPSTAN_VERSION=2.3.1
ARG PHPSTAN_SHA256=85157dba3d95d77ca603d2b7303aa78a538f9e3b56ccad07dd794c53aa96d1ae
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/phpstan
RUN set -eu; \
    curl -fsSL -o phpstan.phar "https://github.com/phpstan/phpstan/releases/download/${PHPSTAN_VERSION}/phpstan.phar"; \
    echo "${PHPSTAN_SHA256}  phpstan.phar" | sha256sum -c -
COPY tools/phpstan/review-phpstan ./

# --- go-toolchain: the pinned Go toolchain golangci-lint needs (it always loads
# packages through `go list`; plan-phase-9 Decisions 4). Official tarball verified
# with `sha256sum -c` against digests committed here (from go.dev/dl). Pruned to
# what loading and type-checking needs: the `go` command, the standard-library
# sources, and the `compile`/`asm`/`link` tools `go list -export` invokes; the
# compiler's own sources, docs, tests and the other tools are dropped (the run
# is verified against this exact layout by adapter/analyzers/test_golangci_lint.py
# in the `dev` image). Only reaches `dev` and the worker-only `worker-runtime`.
# Keep GO_VERSION in step with ci.yml's "Install Go + golangci-lint" step and
# with the language version in adapter/analyzers/golangci_lint.py's go.mod.
FROM debian:bookworm-slim AS go-toolchain
ARG TARGETARCH
ARG GO_VERSION=1.27.2
ARG GO_SHA256_AMD64=ecbadb99091a3f46e31f5f934b068b1864eafa7995211b39eaddf76996045fe5
ARG GO_SHA256_ARM64=94f3e30b8e374bc285e7dadc11e0865726b9bc6e85b841ccceaabc0214c6b7c8
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /go-dl
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
        amd64) sha="$GO_SHA256_AMD64" ;; \
        arm64) sha="$GO_SHA256_ARM64" ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    archive="go${GO_VERSION}.linux-${TARGETARCH:-amd64}.tar.gz"; \
    curl -fsSL -o "$archive" "https://go.dev/dl/${archive}"; \
    echo "${sha}  ${archive}" | sha256sum -c -; \
    mkdir /go; \
    tar xzf "$archive" --strip-components=1 -C /go; \
    cd /go; \
    rm -rf test doc api misc src/cmd; \
    find src -type d -name testdata -prune -exec rm -rf {} +; \
    find src -type f -name '*_test.go' -delete; \
    find pkg/tool -type f ! -name compile ! -name asm ! -name link -delete; \
    /go/bin/go version

# --- golangci-tool: the pinned golangci-lint v2 binary for adapter/analyzers/
# golangci_lint.py (spec-static-analysis-expansion Unit E). Verified with
# `sha256sum -c` against digests committed here (from the release's
# golangci-lint-<version>-checksums.txt). It must be built with a Go no newer than
# GO_VERSION above (it refuses to lint a module newer than its own toolchain). Keep
# the version in step with ci.yml's "Install Go + golangci-lint" step.
FROM debian:bookworm-slim AS golangci-tool
ARG TARGETARCH
ARG GOLANGCI_LINT_VERSION=2.15.0
ARG GOLANGCI_LINT_SHA256_AMD64=9b7fe6a6bf6334b98decf8d86b11415ea7889afdabdd1959fdce23af2a9ebcea
ARG GOLANGCI_LINT_SHA256_ARM64=b4d8f17068b35ee6060cc2f69a16fb48740d706fbba81661c318649c114a6998
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /golangci
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
        amd64) sha="$GOLANGCI_LINT_SHA256_AMD64" ;; \
        arm64) sha="$GOLANGCI_LINT_SHA256_ARM64" ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    archive="golangci-lint-${GOLANGCI_LINT_VERSION}-linux-${TARGETARCH:-amd64}.tar.gz"; \
    curl -fsSL -o "$archive" \
        "https://github.com/golangci/golangci-lint/releases/download/v${GOLANGCI_LINT_VERSION}/${archive}"; \
    echo "${sha}  ${archive}" | sha256sum -c -; \
    tar xzf "$archive" --strip-components=1 "golangci-lint-${GOLANGCI_LINT_VERSION}-linux-${TARGETARCH:-amd64}/golangci-lint"; \
    ./golangci-lint --version

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
COPY --from=ruff /ruff/ruff /usr/local/bin/ruff
COPY --from=trivy /trivy/trivy /usr/local/bin/trivy
COPY --from=semgrep-tool /opt/semgrep /opt/semgrep
RUN ln -s /opt/semgrep/bin/semgrep /usr/local/bin/semgrep
# Node + ESLint so `pytest -m integration` against this target runs the real
# engine (the shipped copy lives only in `worker-runtime` below).
COPY --from=node-runtime /node/bin/node /usr/local/bin/node
COPY --from=eslint-tool /opt/eslint /opt/eslint
RUN ln -s /opt/eslint/review-eslint /usr/local/bin/review-eslint
# PHP + PHPStan for the same reason; libonig5 is the one shared library the
# minimal PHP build links beyond libc.
RUN apt-get update && apt-get install -y --no-install-recommends libonig5 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=php-runtime /opt/php/bin/php /usr/local/bin/php
COPY --from=phpstan-tool /opt/phpstan /opt/phpstan
RUN ln -s /opt/phpstan/review-phpstan /usr/local/bin/review-phpstan
# Go + golangci-lint likewise (the pruned toolchain; `go` on PATH via a symlink,
# GOROOT is found from the real binary location).
COPY --from=go-toolchain /go /usr/local/go
RUN ln -s /usr/local/go/bin/go /usr/local/bin/go
COPY --from=golangci-tool /golangci/golangci-lint /usr/local/bin/golangci-lint
COPY --from=embedding-model /model /app/models/all-MiniLM-L6-v2
COPY . .
ENV PATH="/app/.venv/bin:${PATH}"

# --- runtime: production dependencies + app code only, no dev tooling ---
FROM base AS runtime
COPY --from=deps /app/.venv /app/.venv
COPY --from=gitleaks /usr/bin/gitleaks /usr/local/bin/gitleaks
COPY --from=ruff /ruff/ruff /usr/local/bin/ruff
COPY --from=trivy /trivy/trivy /usr/local/bin/trivy
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
# production image (plan Phase 4, Assumption 12).
RUN rm -rf ./adapter/product_knowledge/testing
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

# --- worker-runtime: `runtime` plus the interpreters the static-analysis
# engines need (plan-phase-9 Decisions 1, 2 and 4): Node + ESLint, PHP +
# PHPStan, Go + golangci-lint. Worker-only by design: the
# internet-facing Webhook and Admin API stay on `runtime` and gain no interpreter. Compose and
# the Helm `worker` Deployment point at this target. It has to come after
# `runtime` (it extends it), so it is the last stage: always build with an
# explicit --target.
FROM runtime AS worker-runtime
USER root
COPY --from=node-runtime /node/bin/node /usr/local/bin/node
COPY --from=eslint-tool /opt/eslint /opt/eslint
RUN ln -s /opt/eslint/review-eslint /usr/local/bin/review-eslint
# PHP + PHPStan for the same reason; libonig5 is the one shared library the
# minimal PHP build links beyond libc.
RUN apt-get update && apt-get install -y --no-install-recommends libonig5 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=php-runtime /opt/php/bin/php /usr/local/bin/php
COPY --from=phpstan-tool /opt/phpstan /opt/phpstan
RUN ln -s /opt/phpstan/review-phpstan /usr/local/bin/review-phpstan
# Go + golangci-lint likewise (the pruned toolchain; `go` on PATH via a symlink,
# GOROOT is found from the real binary location).
COPY --from=go-toolchain /go /usr/local/go
RUN ln -s /usr/local/go/bin/go /usr/local/bin/go
COPY --from=golangci-tool /golangci/golangci-lint /usr/local/bin/golangci-lint
USER worker
