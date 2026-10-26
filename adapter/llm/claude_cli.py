"""adapter.llm.claude_cli — ``llmgateway.client.LLMClient`` backed by the developer's own logged-in
Claude CLI (local mode, US-L7; plan Phase 3b).

**B4 IS UNVERIFIED.** Whether Anthropic's terms permit driving a subscription through the CLI this
way has not been established. This backend is built, is an explicit opt-in
(``REVIEW_LOCAL_BACKEND=claude_cli``) and is never the default; nothing here claims compliance.

**The CLI is an agent and its prompt (the diff) is attacker-influenced.** The invocation is
hardened accordingly. Every flag below was read from ``claude --help`` (Claude Code 2.1.308, the
version on the host when this was written), not guessed:

- ``-p`` / ``--print``: non-interactive, print the response and exit.
- ``--output-format json``: one JSON result object on stdout (parsed and validated below).
- ``--input-format text``: the prompt is plain text on stdin (never argv: argv is visible to every
  local user in ``ps`` and may be logged).
- ``--tools ""``: "use \"\" to disable all tools" (no Bash, no file tools, no web).
- ``--permission-mode dontAsk`` and ``--permission-prompts none``: nothing may prompt, and
  anything that would prompt is denied. No flag that skips or bypasses permissions is ever passed
  (``--dangerously-skip-permissions``, ``--allow-dangerously-skip-permissions`` and
  ``bypassPermissions`` are asserted absent by a test).
- ``--safe-mode``: starts with CLAUDE.md, skills, plugins, hooks, MCP servers, custom commands and
  agents disabled; auth and model selection still work. ``--bare`` is deliberately NOT used: it
  never reads OAuth/keychain credentials, which defeats using a subscription.
- ``--strict-mcp-config`` (with no ``--mcp-config``): no MCP server from any configuration.
- ``--no-session-persistence``: nothing about the review is saved to disk as a resumable session.
- ``--disable-slash-commands``, and a fixed ``--system-prompt``.
- ``--model <alias>`` only when the caller named one.

The working directory is a fresh empty temporary directory, never the repository. The environment
is an allow-list (PATH, HOME, locale, proxy and CA settings, XDG/CLAUDE_CONFIG_DIR): no
``GITHUB_TOKEN``, no model API key (``ANTHROPIC_API_KEY`` and every ``*_API_KEY``/``*_TOKEN``), so
the run cannot read or bill an API key, and the login it uses is only the CLI's own stored one.

``max_tokens`` and ``temperature`` have no CLI equivalent and are ignored. Token usage is always
unreported (``None``): the CLI's own counters are not API token counts.

Failures are typed ``llmgateway.exceptions`` errors that name the backend: a missing executable or
a logged-out CLI is ``ClaudeCliUnavailableError`` (raised by ``preflight`` before any content is
sent, and again by ``complete`` if the login lapses mid-run); a hung CLI is killed and reaped by
``run_analyzer_subprocess`` (the one shared mechanism) and reported as unreachable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from adapter.analyzers.exceptions import (
    AnalyzerInvocationError,
    AnalyzerOutputTooLargeError,
    AnalyzerTimeoutError,
)
from adapter.analyzers.subprocess_runner import (
    cleanup_temp_directory,
    run_analyzer_subprocess,
)
from llmgateway.exceptions import LLMMalformedResponseError, LLMUnreachableError
from llmgateway.models import CompletionRequest, CompletionResponse

logger = logging.getLogger(__name__)

BACKEND_NAME = "Claude CLI"
# ``CompletionRequest.model`` value meaning "the CLI's own default model": no ``--model`` flag.
DEFAULT_MODEL_LABEL = "claude-cli-default"

_SYSTEM_PROMPT = (
    "You are a code review model. Answer only with the output the user's message asks for. "
    "You have no tools; do not try to use any."
)
_MAX_OUTPUT_BYTES = 4_000_000
_PREFLIGHT_TIMEOUT_SECONDS = 15.0

# Allow-list of environment variable names (exact) the CLI child may see. Nothing credential-like.
_ENV_ALLOW = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        # Windows: where the CLI finds its profile and credentials, and how it spawns children.
        "SystemRoot",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "TEMP",
        "TMP",
        "PATHEXT",
        "COMSPEC",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "CLAUDE_CONFIG_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    }
)
_NOT_RUNNABLE = "the Claude CLI executable could not be started"
_LOGGED_OUT_MARKERS = ("not logged in", "please run /login")


class ClaudeCliUnavailableError(LLMUnreachableError):
    """The CLI is missing or not logged in. ``reason`` is ``"missing"`` or ``"logged_out"``."""

    def __init__(self, *, reason: Literal["missing", "logged_out"], detail: str) -> None:
        super().__init__(endpoint=BACKEND_NAME, detail=detail)
        self.reason = reason


class _CliResult(BaseModel):
    """The one JSON object ``--output-format json`` prints; fields beyond these are ignored."""

    model_config = ConfigDict(extra="ignore")
    type: Literal["result"]
    is_error: bool = False
    result: str | None = None  # error results (error_max_turns, ...) may omit it


_ENV_ALLOW_UPPER = frozenset(name.upper() for name in _ENV_ALLOW)


def claude_cli_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The child environment: ``environ`` reduced to the allow-list above. Matched
    case-insensitively because ``os.environ`` upper-cases names on Windows (``SystemRoot`` arrives
    as ``SYSTEMROOT``)."""
    return {k: v for k, v in environ.items() if k.upper() in _ENV_ALLOW_UPPER}


_ROLE_MARKER = re.compile(r"\[(system|user|assistant|tool)\]", re.IGNORECASE)


def render_prompt(request: CompletionRequest) -> str:
    """The conversation as one text prompt for stdin. Stdin has no structural role boundary, so a
    line in the content that looks like a role header (``[system]``) is escaped: diff text must
    not be able to forge a turn."""
    return "\n\n".join(
        f"[{m.role}]\n{_ROLE_MARKER.sub(r'\\\g<0>', m.content)}" for m in request.messages
    )


def build_argv(executable: str, *, model: str | None) -> list[str]:
    """The hardened invocation (see the module docstring for what each flag is and why)."""
    argv = [
        executable,
        "-p",
        "--output-format",
        "json",
        "--input-format",
        "text",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--safe-mode",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--system-prompt",
        _SYSTEM_PROMPT,
    ]
    if model is not None and model != DEFAULT_MODEL_LABEL:
        argv.append(f"--model={model}")  # one token: a value starting with "-" is not a flag
    return argv


def _looks_logged_out(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _LOGGED_OUT_MARKERS)


def parse_result(stdout: bytes) -> str:
    """The model's text from the CLI's JSON result, or a typed error. An ``is_error`` result is
    the CLI reporting a failure (a lapsed login is the common one), never model output."""
    try:
        result = _CliResult.model_validate(json.loads(stdout))
    except (ValueError, ValidationError) as exc:
        raise LLMMalformedResponseError(
            endpoint=BACKEND_NAME, detail="output was not the expected JSON result object"
        ) from exc
    if result.is_error:
        if _looks_logged_out(result.result or ""):
            raise ClaudeCliUnavailableError(
                reason="logged_out", detail="not logged in; run `claude` and sign in"
            )
        raise LLMUnreachableError(endpoint=BACKEND_NAME, detail="the CLI reported an error")
    if result.result is None:
        raise LLMMalformedResponseError(
            endpoint=BACKEND_NAME, detail="output was not the expected JSON result object"
        )
    return result.result


@contextlib.asynccontextmanager
async def _scratch_directory() -> AsyncIterator[Path]:
    """An empty working directory for one CLI call, removed afterwards."""
    directory = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="review-claude-"))
    try:
        yield directory
    finally:
        await cleanup_temp_directory(directory)


class ClaudeCliClient:
    """``LLMClient`` over the ``claude`` executable. ``environ`` is the caller's environment (the
    composition root passes it; this adapter never reads ``os.environ`` itself)."""

    def __init__(self, *, executable: str = "claude", environ: Mapping[str, str]) -> None:
        self._executable = executable
        self._env = claude_cli_env(environ)

    def _resolve(self) -> str:
        found = shutil.which(self._executable, path=self._env.get("PATH"))
        if found is None:
            raise ClaudeCliUnavailableError(
                reason="missing",
                detail=f"`{self._executable}` was not found on PATH; install the Claude CLI",
            )
        return found

    async def preflight(self) -> None:
        """Fail before any content is sent: the executable exists and ``claude auth status``
        reports a login. Its output (it names the account) is discarded, never logged."""
        executable = self._resolve()
        try:
            async with _scratch_directory() as directory:
                output = await run_analyzer_subprocess(
                    [executable, "auth", "status", "--json"],
                    cwd=directory,
                    timeout=_PREFLIGHT_TIMEOUT_SECONDS,
                    max_output_bytes=100_000,
                    stdin=b"",
                    merge_stderr=False,
                    isolated=True,  # a timeout must also kill anything the CLI spawned
                    env=self._env,
                )
        except AnalyzerInvocationError as exc:
            # The CLI exits non-zero when signed out, but also when it is broken: say both.
            raise ClaudeCliUnavailableError(
                reason="logged_out",
                detail="not logged in, or the CLI is broken (`claude auth status` failed); sign in",
            ) from exc
        except (AnalyzerTimeoutError, AnalyzerOutputTooLargeError) as exc:
            raise LLMUnreachableError(
                endpoint=BACKEND_NAME, detail="`claude auth status` did not answer in time"
            ) from exc
        except OSError as exc:  # found on PATH but cannot be executed
            raise ClaudeCliUnavailableError(reason="missing", detail=_NOT_RUNNABLE) from exc
        try:
            logged_in = json.loads(output).get("loggedIn", True)
        except (ValueError, AttributeError):
            logged_in = True  # a zero exit is the signal; the JSON is only a second opinion
        if logged_in is False:
            raise ClaudeCliUnavailableError(
                reason="logged_out", detail="not logged in; run `claude` and sign in"
            )

    async def complete(self, request: CompletionRequest, *, timeout: float) -> CompletionResponse:
        executable = self._resolve()
        started = time.monotonic()
        try:
            async with _scratch_directory() as directory:
                stdout = await run_analyzer_subprocess(
                    build_argv(executable, model=request.model),
                    cwd=directory,
                    timeout=timeout,
                    max_output_bytes=_MAX_OUTPUT_BYTES,
                    stdin=render_prompt(request).encode(),
                    merge_stderr=False,
                    isolated=True,  # a timeout must also kill anything the CLI spawned
                    env=self._env,
                )
        except AnalyzerTimeoutError as exc:
            raise LLMUnreachableError(
                endpoint=BACKEND_NAME,
                detail=f"no answer within {timeout:g}s; the process was killed",
            ) from exc
        except AnalyzerOutputTooLargeError as exc:
            raise LLMMalformedResponseError(
                endpoint=BACKEND_NAME, detail="output exceeded the size cap"
            ) from exc
        except OSError as exc:  # found on PATH but cannot be executed
            raise ClaudeCliUnavailableError(reason="missing", detail=_NOT_RUNNABLE) from exc
        except AnalyzerInvocationError as exc:
            # The CLI's own error text (never the prompt) tells a lapsed login from other failures.
            # stdout (the JSON error) comes first and stderr follows, so check both ends.
            if _looks_logged_out(
                (exc.output[:2000] + exc.output[-2000:]).decode("utf-8", "replace")
            ):
                raise ClaudeCliUnavailableError(
                    reason="logged_out", detail="not logged in; run `claude` and sign in"
                ) from exc
            raise LLMUnreachableError(
                endpoint=BACKEND_NAME, detail=f"the CLI exited with status {exc.returncode}"
            ) from exc
        logger.info(
            "claude cli completion finished",
            extra={
                "backend": BACKEND_NAME,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return CompletionResponse(
            content=parse_result(stdout), model=request.model, finish_reason=None, usage=None
        )
