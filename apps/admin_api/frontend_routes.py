"""apps.admin_api.frontend_routes — serves the built React frontend
(``apps/admin_api/frontend/``, Assumption 9) as static files, per
``docs/plans/plan-deliver-admin-gui.md``'s Phase 8 section (Unit I).

A single catch-all ``GET /{full_path:path}`` route, registered in
``apps/admin_api/main.py`` **after** every API router — FastAPI/Starlette
tries routes in registration order, so every ``/api/v1/...``/``/healthz``
request is already claimed by an earlier, more specific route before this
catch-all is ever reached. This route additionally rejects any request
whose path still starts with ``api/`` or is exactly ``healthz`` (a defense-
in-depth check, not the only thing enforcing the exclusion — belt and
braces against a future routing refactor that changes registration order)
— a malformed/unknown ``/api/...`` path gets a real 404, never the SPA's
``index.html``, which would otherwise mask a broken API call as a
confusing blank page.

``dist_dir`` doesn't exist in every environment this app runs in (most
notably the `dev`/`runtime` Python Docker stages before the Dockerfile's
own ``frontend-build`` stage output is copied in, and the bare `dev` image
this repo's test/lint convention builds — the frontend is built in its own
Node stage, never inside the Python image) — ``create_frontend_router``
returns ``None`` in that case rather than raising, so ``apps/admin_api``
boots and every API route keeps working with no frontend mounted, the same
"never a hard dependency" posture this plan's own Phase 8 "Observability &
rollback" section describes (a broken frontend build fails CI, never a
running deployment) extended to "frontend not built at all yet."
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

_INDEX_FILENAME = "index.html"


def resolve_safe_static_path(dist_dir: Path, full_path: str) -> Path | None:
    """Resolves ``full_path`` (caller-controlled — the raw catch-all path
    segment) against ``dist_dir``, returning the resolved file path only
    when it both exists as a file and stays within ``dist_dir`` — never a
    path that escapes it via a ``..`` segment. Returns ``None`` for
    anything else (including a directory), which the caller falls back to
    ``index.html`` for.

    A standalone, pure function (no I/O beyond the two filesystem checks
    below, no FastAPI/Starlette types) specifically so
    ``apps/admin_api/test_frontend_routes.py`` can unit-test the
    traversal guard directly with an arbitrary string — an HTTP round trip
    through an ASGI test client can't reliably exercise this, since
    well-behaved HTTP clients normalize a literal ``..`` segment out of a
    URL before it's ever sent.
    """
    resolved_dist_dir = dist_dir.resolve()
    candidate = (dist_dir / full_path).resolve()
    is_within_dist_dir = candidate == resolved_dist_dir or resolved_dist_dir in candidate.parents
    if is_within_dist_dir and candidate.is_file():
        return candidate
    return None


def create_frontend_router(dist_dir: Path) -> APIRouter | None:
    """Builds the frontend catch-all router, or returns ``None`` when
    ``dist_dir`` (or its ``index.html``) doesn't exist — see this module's
    own docstring for why that's an expected, non-fatal state in some
    environments.
    """
    index_path = dist_dir / _INDEX_FILENAME
    if not index_path.is_file():
        logger.info(
            "frontend dist not found; skipping frontend mount (expected when the "
            "frontend hasn't been built in this environment)",
            extra={"dist_dir": str(dist_dir)},
        )
        return None

    router = APIRouter()

    @router.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str) -> FileResponse:
        # Belt-and-braces (see module docstring) — every real /api/*
        # and /healthz request is already claimed by an earlier-registered
        # route before this catch-all is ever reached.
        if full_path.startswith("api/") or full_path == "healthz":
            raise HTTPException(status_code=404, detail="not found")

        candidate = resolve_safe_static_path(dist_dir, full_path)
        if candidate is not None:
            return FileResponse(candidate)

        # Any other path (an SPA client-side route like `/dashboard`, an
        # unknown path, or a rejected traversal attempt) falls back to
        # `index.html` — react-router resolves the real screen
        # client-side.
        return FileResponse(index_path)

    return router
