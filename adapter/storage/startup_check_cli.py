"""adapter.storage.startup_check_cli — standalone CLI entrypoint for Phase
2's pgvector-presence check, invoked by Docker Compose's ``migrate`` service
(see ``docker-compose.yml``) immediately after ``alembic upgrade head``.

Per the plan's Phase 5 section: Compose must genuinely block until "Alembic
migrations applied and Phase 2's pgvector-presence check passing" — this
module is the second half of that gate. A missing ``pgvector`` extension is
caught here, at compose-boot time, with a clear non-zero exit and a
structured log line, rather than surfacing as an obscure query failure the
first time a later phase's vector columns are used.

Reads ``REVIEW_DATABASE_URL`` directly from the environment. This mirrors
the documented exception in ``adapter/storage/migrations/env.py``'s module
docstring to docs/rules/architecture.md's "never read env vars from adapter
code" rule: this is a standalone ops CLI invoked by Compose/CI, not
request/job-handling application code wired through ``pydantic-settings``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from adapter.storage.engine import create_storage_engine
from adapter.storage.exceptions import PgVectorNotInstalledError
from adapter.storage.pgvector_check import check_pgvector_installed

logger = logging.getLogger(__name__)


async def _check(dsn: str) -> None:
    engine = create_storage_engine(dsn)
    try:
        await check_pgvector_installed(engine)
    finally:
        await engine.dispose()


def main() -> None:
    """Run the pgvector-presence check and exit non-zero with a clear,
    structured log line on failure.

    Per the plan's Phase 5 requirement, "a migration failure must make the
    affected service exit non-zero with the error visible in `docker
    compose logs`, never a silent hang" — this function is the second
    half of the `migrate` service's command (after `alembic upgrade head`,
    see docker-compose.yml), completing that same fail-fast contract for
    the pgvector-presence half of "migrations applied".
    """
    logging.basicConfig(
        level="INFO", format="%(levelname)s [%(name)s] %(message)s", stream=sys.stdout
    )
    dsn = os.environ.get("REVIEW_DATABASE_URL")
    if not dsn:
        logger.error("REVIEW_DATABASE_URL must be set to run the pgvector startup check")
        sys.exit(1)

    try:
        asyncio.run(_check(dsn))
    except PgVectorNotInstalledError as exc:
        logger.error("pgvector startup check failed: %s", exc)
        sys.exit(1)

    logger.info("pgvector extension present; migrations complete")


if __name__ == "__main__":
    main()
