"""adapter.local.environment — keep CI and model credentials out of every child process.

``adapter.analyzers.subprocess_runner.analyzer_env`` (shared with server mode, and left alone)
strips only ``REVIEW_`` and ``OTEL_`` variables. In a CI job the runner also provides
``GITHUB_TOKEN``, ``ACTIONS_RUNTIME_TOKEN`` and friends, and a developer's shell may export
``GH_TOKEN`` or a cloud key. The git, gitleaks and analyzer subprocesses of a local run read
attacker-influenced repository content (hooks, filters, semgrep and project configs), so none of
them may inherit those.

``scrubbed_environment`` returns a copy of the given environment without them. The composition
root builds it once and passes that one mapping to every adapter that spawns a child (git, the
analyzers, the Claude CLI, MCP stdio servers); no adapter reads ``os.environ`` and nothing is
mutated, so concurrent runs cannot see each other's scrub. Configuration (``LocalSettings``) is
parsed from the real environment before this, so the run still has the values it needs.
"""

from __future__ import annotations

from collections.abc import Mapping

# Names that are credentials wherever they appear.
_EXACT = frozenset(
    {
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "ACTIONS_CACHE_URL",
        "ACTIONS_RESULTS_URL",
        # Handles and hooks that let a child use or run a credential (an agent socket, a helper).
        "SSH_AUTH_SOCK",
        "SSH_ASKPASS",
        "GIT_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
    }
)
# Suffixes that mark a credential by convention (GITLAB_TOKEN, AWS_SECRET_ACCESS_KEY, ...).
_SUFFIXES = (
    # No leading underscore: Azure's SYSTEM_ACCESSTOKEN runs the word into its prefix.
    "TOKEN",
    "_JWT",
    "_JWT_V2",  # GitLab CI_JOB_JWT_V2
    "_SECRET",
    # No leading underscore: PGPASSWORD, MYSQL_PWD and similar run the word into the prefix.
    "PASSWORD",
    "PASSWD",
    "_PWD",
    "_DSN",
    "_AUTH",
    "_API_KEY",
    "_APIKEY",
    "_ACCESS_KEY",
    "_SECRET_KEY",
    "_PRIVATE_KEY",
    "_CREDENTIALS",
    "_PAT",
)


def is_credential_name(name: str) -> bool:
    upper = name.upper()
    return upper in _EXACT or upper.endswith(_SUFFIXES)


def scrubbed_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``environ`` without credential-named variables."""
    return {name: value for name, value in environ.items() if not is_credential_name(name)}
