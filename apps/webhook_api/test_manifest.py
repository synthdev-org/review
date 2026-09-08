"""Unit tests for ``apps.webhook_api.manifest_routes``'s pure,
no-I/O ``_build_app_manifest`` helper.

Per docs/rules/testing.md's pyramid, this is a Unit test (no I/O, no
FastAPI ``TestClient``) — kept separate from ``test_manifest_routes.py``,
which is Integration-level (drives the real app via ``TestClient`` with
GitHub's code-exchange endpoint stubbed via ``respx``).

Per ``docs/plans/plan-deliver-memory.md``'s Phase 7 section (Unit G): the
plan's own Tests bullet names this coverage as
"``adapter/github/test_manifest.py``, extended" — a carried-over
inaccuracy from ``docs/specs/spec-memory.md``'s Unit G description.
``_build_app_manifest`` has always lived in
``apps/webhook_api/manifest_routes.py`` (it needs the request's
``base_url``, a FastAPI-layer concern), never in the I/O-free
``adapter/github/manifest.py`` module (which only holds the setup-state
CSRF helpers and the code-exchange call) — this test is added where the
function it exercises actually lives, not moved to match the plan's
mis-stated path.
"""

from __future__ import annotations

from apps.webhook_api.manifest_routes import _build_app_manifest


def test_build_app_manifest_default_events_is_exactly_the_expected_set() -> None:
    """``docs/plans/plan-deliver-memory.md``'s Phase 7 section: the manifest
    gains ``"pull_request_review_comment"``/``"pull_request_review_thread"``
    (Phases 3/4's new webhook subscriptions) on top of the already-subscribed
    ``"pull_request"``/``"push"`` — and ``"issue_comment"`` is deliberately
    **not** added, per Assumption 3 (an ``issue_comment`` event can never
    correlate back to a specific posted finding comment).
    """
    manifest = _build_app_manifest(
        base_url="https://review.example.com", name="review (self-hosted)"
    )

    assert manifest["default_events"] == [
        "pull_request",
        "push",
        "pull_request_review_comment",
        "pull_request_review_thread",
    ]
    assert "issue_comment" not in manifest["default_events"]
