"""E2E happy path: `docker compose up` reaches a genuinely healthy
state with no crash-looping — the literal acceptance test for the
roadmap's Phase 0 exit criteria ("a Webhook API + Worker + Postgres that
boots cleanly with no review capability yet").

Asserts, over real HTTP/Docker state (never by importing application code
directly — see docs/rules/testing.md's "no shortcuts that bypass the
Webhook API's actual entrypoint" E2E rule):
  * `apps/webhook_api`'s `/healthz` returns 200.
  * `alembic upgrade head` (plus the pgvector-presence check) has run
    against the compose Postgres instance — verified two ways: the
    `migrate` service itself exited 0, and the expected baseline schema
    (the `org` table the baseline migration creates) is actually queryable.
  * `apps/worker` is running without crash-looping: since it runs its
    claim/dispatch loop (which runs forever by design — see
    `apps/worker/dispatch.py`'s own docstring), a clean, intentional
    *non-return* is now the healthy signal, not the earlier "exits 0"
    signal this test previously asserted (see docker-compose.yml's
    `worker` service comment for the full before/after). The container
    logs "worker started" and then stays in the `running` state, never
    restarting, for the test's observation window.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Iterator

import pytest

from tests.e2e.conftest import (
    base_compose_env,
    compose,
    compose_down,
    container_id,
    wait_for_exit,
    wait_for_healthy,
    wait_for_log_line,
    wait_for_running_stable,
)


@pytest.fixture
def running_stack(stub_llm_server: object) -> Iterator[dict[str, str]]:
    env = base_compose_env()
    compose_down(env)  # clean slate: no volume/container left by a prior run
    result = compose("up", "-d", "--build", env=env)
    assert result.returncode == 0, (
        f"docker compose up failed unexpectedly.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    try:
        yield env
    finally:
        logs = compose("logs", "--no-color", env=env).stdout
        print(logs)  # surfaced by pytest on failure via -s/--capture=no, or -v tail
        compose_down(env)


def test_webhook_api_healthz_returns_200(running_stack: dict[str, str]) -> None:
    env = running_stack
    wait_for_healthy("webhook_api", env)

    port = env["WEBHOOK_API_PORT"]
    with urllib.request.urlopen(f"http://localhost:{port}/healthz", timeout=5) as response:
        assert response.status == 200
        assert response.read() == b'{"status":"ok"}'


def test_migrations_and_pgvector_check_applied(running_stack: dict[str, str]) -> None:
    env = running_stack

    # `docker compose up` already blocked on `migrate` succeeding (it's a
    # `depends_on: condition: service_completed_successfully` dependency of
    # both webhook_api and worker) — assert that directly rather than just
    # trusting the fixture's overall returncode==0 check.
    exit_code = wait_for_exit("migrate", env)
    assert exit_code == 0

    logs = compose("logs", "--no-color", "migrate", env=env).stdout
    assert "pgvector extension present" in logs

    # And independently confirm the schema is actually there: the
    # baseline migration creates the `org` table.
    postgres_cid = container_id("postgres", env)
    assert postgres_cid, "postgres container not found"
    check = compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        env["POSTGRES_USER"],
        "-d",
        env["POSTGRES_DB"],
        "-tAc",
        "SELECT to_regclass('public.org') IS NOT NULL",
        env=env,
    )
    assert check.returncode == 0, check.stderr
    assert check.stdout.strip() == "t"


def test_worker_boots_cleanly_without_crash_looping(running_stack: dict[str, str]) -> None:
    env = running_stack

    # Per docker-compose.yml's `worker` service comment: the
    # claim/dispatch loop runs forever by design once main()'s startup
    # checks pass — "still running", not "exited 0", is the healthy signal
    # from here on. `restart: unless-stopped` plus a stable
    # RestartCount over the observation window together are the actual
    # "no crash-looping" proof: a real startup-check failure would exit 1
    # (see apps/worker/main.py's `SystemExit(1)` on LLM unreachable) and,
    # under this restart policy, keep restarting — neither happens here.
    wait_for_log_line("worker", env, "worker started")

    logs = compose("logs", "--no-color", "worker", env=env).stdout
    assert "llm endpoint reachable" in logs
    assert "worker started" in logs

    wait_for_running_stable("worker", env)


def test_worker_runs_as_non_root(running_stack: dict[str, str]) -> None:
    """Proves the built image actually runs as non-root, not only
    that the Dockerfile source says so — a regression here (e.g. a future
    edit accidentally dropping the ``USER`` line) would otherwise go
    undetected until an actual incident.
    """
    env = running_stack
    wait_for_log_line("worker", env, "worker started")

    result = compose("exec", "-T", "worker", "id", "-u", env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != "0", "worker container is running as root (uid 0)"
