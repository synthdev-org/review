"""Unit tests for ``apps.webhook_api.manifest_routes``'s pure,
no-I/O ``_build_app_manifest`` helper.

Per docs/rules/testing.md's pyramid, this is a Unit test (no I/O, no
FastAPI ``TestClient``) — kept separate from ``test_manifest_routes.py``,
which is Integration-level (drives the real app via ``TestClient`` with
GitHub's code-exchange endpoint stubbed via ``respx``).

``_build_app_manifest`` lives in ``apps/webhook_api/manifest_routes.py`` (it
needs the request's ``base_url``, a FastAPI-layer concern), not in the
I/O-free ``adapter/github/manifest.py`` module (which only holds the
setup-state CSRF helpers and the code-exchange call), so this test sits
beside it.
"""

from __future__ import annotations

from apps.webhook_api.manifest_routes import _build_app_manifest


def test_build_app_manifest_default_events_is_exactly_the_expected_set() -> None:
    """The manifest subscribes to ``"pull_request_review_comment"``/
    ``"pull_request_review_thread"`` on top of ``"pull_request"``/``"push"``
    — and ``"issue_comment"`` is deliberately **not** subscribed (an
    ``issue_comment`` event can never correlate back to a specific posted
    finding comment).
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
