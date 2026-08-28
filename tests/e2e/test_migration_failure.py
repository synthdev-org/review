"""Phase 5 E2E negative case: a migration failure must exit non-zero with
the error visible in `docker compose logs`, never a silent hang — per the
plan's Phase 5 "Tests" and "Acceptance" sections ("A second E2E case forces
a migration failure ... and asserts the failure is visible in `docker
compose logs` rather than a silent hang").

`REVIEW_DATABASE_URL` is overridden to a value SQLAlchemy cannot parse at
all (`not-a-valid-database-url`), so `alembic upgrade head` fails
immediately and deterministically inside the `migrate` container, with no
dependency on Postgres connectivity/auth timing — the fastest, least flaky
way to force this failure mode in CI.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.e2e.conftest import (
    base_compose_env,
    compose,
    compose_down,
    container_id,
    docker_inspect,
    wait_for_exit,
)


@pytest.fixture
def broken_migration_env(stub_llm_server: object) -> Iterator[dict[str, str]]:
    env = base_compose_env(REVIEW_DATABASE_URL="not-a-valid-database-url")
    compose_down(env)
    try:
        yield env
    finally:
        logs = compose("logs", "--no-color", env=env).stdout
        print(logs)
        compose_down(env)


def test_invalid_database_url_fails_migrate_loudly(broken_migration_env: dict[str, str]) -> None:
    env = broken_migration_env

    result = compose("up", "-d", "--build", env=env)

    # Compose refuses to start webhook_api/worker at all once their
    # `depends_on: migrate: condition: service_completed_successfully`
    # dependency fails — `up` itself reports this as a non-zero exit,
    # which is the first, loudest signal this is not a silent hang.
    assert result.returncode != 0, (
        f"docker compose up should have failed when migrate's dependency "
        f"failed, but it returned 0.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    exit_code = wait_for_exit("migrate", env)
    assert exit_code != 0

    logs = compose("logs", "--no-color", "migrate", env=env).stdout
    # SQLAlchemy's `ArgumentError` for an unparseable URL deliberately does
    # *not* echo the raw URL string back (avoids leaking credentials from a
    # malformed-but-otherwise-real DSN into logs) — asserting on the
    # specific exception type/message it does print proves the *real*
    # error is what's visible, not a generic timeout or a swallowed
    # exception.
    assert "sqlalchemy.exc.ArgumentError" in logs
    assert "Could not parse SQLAlchemy URL" in logs

    # And confirm no silent partial boot: Compose creates the dependent
    # containers up front but must never start them once `migrate` (their
    # `depends_on: condition: service_completed_successfully` dependency)
    # has failed.
    for service in ("webhook_api", "worker"):
        cid = container_id(service, env, all_states=True)
        assert cid, f"expected a created (but not started) {service} container"
        status = docker_inspect(cid, "{{.State.Status}}")
        assert status == "created", f"{service} should never have started; state was {status!r}"
