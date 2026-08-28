"""apps.webhook_api.manifest_routes — the GitHub App manifest/callback
setup flow, gated behind ``SETUP_MODE``.

Per the plan's Phase 4 section: "The manifest-initiation and callback
routes are both gated behind an explicit ``SETUP_MODE`` config flag, off by
default — a one-time install-time flow the operator enables only for the
duration of App setup ... not a standing production endpoint any caller
could otherwise trigger to provision attacker-supplied App credentials."
Per this phase's Security review: without this gate, any network caller
who can reach ``webhook_api`` could initiate and complete the App creation
flow themselves. ``apps/webhook_api/main.py`` enforces the gate by only
calling ``create_manifest_router`` (registering these routes at all) when
``settings.setup_mode`` is true — an unregistered route 404s, which is a
stronger guarantee than a runtime check inside an always-registered route.

Credentials returned by the callback are handed straight to the operator
in the JSON response body (the intended delivery channel) and are never
written to Postgres or logged — every log call below carries only
non-secret identifiers (``app_id``, ``app_slug``, rejection reasons), per
the plan's Observability & rollback section ("successful credential
exchanges are ... logged ... without ever logging the private key/secret
values themselves").
"""

from __future__ import annotations

import html
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import SecretStr

from adapter.github.exceptions import (
    GitHubCodeExchangeError,
    SetupStateExpiredError,
    SetupStateMismatchError,
    SetupStateMissingError,
)
from adapter.github.manifest import (
    exchange_temporary_code,
    generate_setup_state,
    verify_setup_state,
)

logger = logging.getLogger(__name__)

_STATE_COOKIE_NAME = "review_setup_state"


def create_manifest_router(
    *,
    setup_state_secret: SecretStr,
    state_ttl_seconds: float,
    github_api_base_url: str,
    github_api_timeout_seconds: float,
    app_manifest_name: str = "review (self-hosted)",
) -> APIRouter:
    """Build the ``SETUP_MODE``-gated manifest/callback router.

    A factory closing over its dependencies, per
    docs/rules/architecture.md's constructor-injection rule — only called
    by ``apps/webhook_api/main.py``, and only when ``settings.setup_mode``
    is true (see module docstring).
    """
    router = APIRouter()
    state_secret_bytes = setup_state_secret.get_secret_value().encode("utf-8")

    @router.get("/setup/start")
    async def start_github_app_setup(request: Request) -> HTMLResponse:
        state = generate_setup_state(secret=state_secret_bytes)
        manifest = _build_app_manifest(base_url=str(request.base_url), name=app_manifest_name)
        html_body = _render_manifest_form(manifest=manifest, state=state)

        response = HTMLResponse(html_body)
        response.set_cookie(
            _STATE_COOKIE_NAME,
            state,
            max_age=int(state_ttl_seconds),
            httponly=True,
            samesite="lax",
            # GitHub's manifest flow round-trips through a top-level
            # redirect, which SameSite=Strict would drop the cookie for —
            # see the plan's Phase 4 section for why Lax, not Strict.
            secure=request.url.scheme == "https",
        )
        logger.info("github app setup initiated")
        return response

    @router.get("/setup/callback")
    async def github_app_setup_callback(
        request: Request, code: str | None = None, state: str | None = None
    ) -> JSONResponse:
        cookie_value = request.cookies.get(_STATE_COOKIE_NAME)
        try:
            verify_setup_state(
                cookie_value=cookie_value,
                query_state=state,
                secret=state_secret_bytes,
                ttl_seconds=state_ttl_seconds,
            )
        except SetupStateMissingError:
            logger.warning("github app setup callback rejected", extra={"reason": "missing_state"})
            raise HTTPException(status_code=400, detail="missing state parameter") from None
        except SetupStateMismatchError:
            logger.warning("github app setup callback rejected", extra={"reason": "state_mismatch"})
            raise HTTPException(status_code=400, detail="state parameter mismatch") from None
        except SetupStateExpiredError:
            logger.warning("github app setup callback rejected", extra={"reason": "state_expired"})
            raise HTTPException(status_code=400, detail="state parameter expired") from None

        if not code:
            logger.warning("github app setup callback rejected", extra={"reason": "missing_code"})
            raise HTTPException(status_code=400, detail="missing code parameter")

        try:
            credentials = await exchange_temporary_code(
                code, timeout=github_api_timeout_seconds, api_base_url=github_api_base_url
            )
        except GitHubCodeExchangeError as exc:
            logger.warning(
                "github app setup callback rejected", extra={"reason": "code_exchange_failed"}
            )
            raise HTTPException(
                status_code=502, detail="failed to exchange code with GitHub"
            ) from exc

        logger.info(
            "github app setup callback succeeded",
            extra={"app_id": credentials.app_id, "app_slug": credentials.app_slug},
        )
        response = JSONResponse(
            {
                "app_id": credentials.app_id,
                "app_slug": credentials.app_slug,
                "private_key": credentials.private_key.get_secret_value(),
                "webhook_secret": credentials.webhook_secret.get_secret_value(),
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret.get_secret_value(),
            }
        )
        response.delete_cookie(_STATE_COOKIE_NAME)
        return response

    return router


def _build_app_manifest(*, base_url: str, name: str) -> dict[str, object]:
    """The GitHub App manifest submitted to GitHub's manifest-creation
    form. A minimal viable manifest for this phase's skeleton — enough
    permissions/events to receive PR/push webhooks and (later) post check
    runs/comments; later roadmap phases extend permissions as the review
    pipeline needs more, which requires no change to this flow's plumbing.
    """
    trimmed_base_url = base_url.rstrip("/")
    return {
        "name": name,
        "url": trimmed_base_url,
        "hook_attributes": {"url": f"{trimmed_base_url}/webhooks/github"},
        "redirect_url": f"{trimmed_base_url}/setup/callback",
        "public": False,
        "default_permissions": {
            "checks": "write",
            "contents": "read",
            "pull_requests": "write",
            "metadata": "read",
        },
        "default_events": ["pull_request", "push"],
    }


def _render_manifest_form(*, manifest: dict[str, object], state: str) -> str:
    """An auto-submitting HTML form POSTing ``manifest`` to GitHub's App
    manifest-creation endpoint, per GitHub's documented manifest flow
    (docs.github.com: "Registering a GitHub App from a manifest"). The
    manifest JSON and state value are both HTML-escaped before being
    embedded in the response.
    """
    manifest_json = html.escape(json.dumps(manifest), quote=True)
    escaped_state = html.escape(state, quote=True)
    return (
        "<!doctype html>\n"
        "<html>\n"
        '<body onload="document.forms[0].submit()">\n'
        f'  <form action="https://github.com/settings/apps/new?state={escaped_state}" '
        'method="post">\n'
        f'    <input type="hidden" name="manifest" value="{manifest_json}">\n'
        "  </form>\n"
        "</body>\n"
        "</html>\n"
    )
