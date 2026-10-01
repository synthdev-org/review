"""Unit + lightweight-integration tests for
apps/admin_api/frontend_routes.py, per
docs/plans/plan-deliver-admin-gui.md's Phase 8 section (Unit I,
Assumption 9).

No Postgres/testcontainers needed — this module has zero storage
dependencies, so every test here runs in the default (non-`integration`)
`pytest` tier, against a plain filesystem fixture (`tmp_path`) standing in
for a real `npm run build` output, per this module's own docstring on why
the Python test tier never needs a real frontend build.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.admin_api.frontend_routes import create_frontend_router, resolve_safe_static_path


@pytest.fixture
def dist_dir(tmp_path: Path) -> Path:
    """A fixture standing in for a real `npm run build` output: an
    `index.html` plus one nested static asset, mirroring Vite's own
    `dist/assets/*` layout."""
    (tmp_path / "index.html").write_text("<html><body>admin ui shell</body></html>")
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    (assets_dir / "index-abc123.js").write_text("console.log('built');")
    return tmp_path


def test_create_frontend_router_returns_none_when_dist_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert create_frontend_router(missing) is None


def test_create_frontend_router_returns_none_when_index_html_missing(tmp_path: Path) -> None:
    # A dist_dir that exists but has no index.html (e.g. an interrupted
    # build) is treated the same as "not built" — never a half-served app.
    (tmp_path / "assets").mkdir()
    assert create_frontend_router(tmp_path) is None


def _client_for(dist_dir: Path) -> TestClient:
    app = FastAPI()
    router = create_frontend_router(dist_dir)
    assert router is not None
    app.include_router(router)
    return TestClient(app)


def test_serves_index_html_at_root(dist_dir: Path) -> None:
    client = _client_for(dist_dir)
    response = client.get("/")
    assert response.status_code == 200
    assert "admin ui shell" in response.text


def test_serves_a_real_static_asset(dist_dir: Path) -> None:
    client = _client_for(dist_dir)
    response = client.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_unknown_spa_route_falls_back_to_index_html(dist_dir: Path) -> None:
    # react-router resolves the real screen client-side — every unknown
    # path (a client-side route like /dashboard) gets the SPA shell, a 200,
    # never a 404.
    client = _client_for(dist_dir)
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "admin ui shell" in response.text


def test_paths_still_claimed_by_api_or_healthz_are_rejected_not_served(dist_dir: Path) -> None:
    # Belt-and-braces (per this module's own docstring): even if this
    # catch-all were somehow reached for one of these paths, it must never
    # serve the SPA shell in their place.
    client = _client_for(dist_dir)
    assert client.get("/api/v1/auth/session").status_code == 404
    assert client.get("/healthz").status_code == 404


class TestResolveSafeStaticPath:
    """Unit tests for the pure traversal-guard helper — exercised directly
    with an arbitrary string, not through an HTTP client, since a
    well-behaved HTTP client normalizes a literal `..` segment out of a
    URL before it's ever sent (see the helper's own docstring)."""

    def test_returns_the_resolved_path_for_a_real_file(self, dist_dir: Path) -> None:
        result = resolve_safe_static_path(dist_dir, "assets/index-abc123.js")
        assert result == (dist_dir / "assets" / "index-abc123.js").resolve()

    def test_returns_none_for_a_missing_file(self, dist_dir: Path) -> None:
        assert resolve_safe_static_path(dist_dir, "assets/does-not-exist.js") is None

    def test_returns_none_for_a_directory(self, dist_dir: Path) -> None:
        assert resolve_safe_static_path(dist_dir, "assets") is None

    def test_rejects_a_traversal_attempt_outside_dist_dir(self, dist_dir: Path) -> None:
        # A secret file that exists, but one directory above dist_dir —
        # proves the guard rejects it even though the *target* file is
        # real, not just a missing-file coincidence.
        secret = dist_dir.parent / "secret.txt"
        secret.write_text("outside dist_dir")

        assert resolve_safe_static_path(dist_dir, "../secret.txt") is None
        assert resolve_safe_static_path(dist_dir, "assets/../../secret.txt") is None
