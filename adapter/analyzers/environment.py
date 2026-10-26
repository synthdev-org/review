"""adapter.analyzers.environment — the child-environment injection point the subprocess-backed
analyzers share (ADR-0016: the composition root chooses the environment, adapters never read it)."""

from __future__ import annotations

from collections.abc import Mapping


class ChildEnvironmentMixin:
    """Holds the complete child environment chosen by the composition root. ``None`` keeps
    ``run_analyzer_subprocess``'s default (``os.environ`` minus Worker credentials)."""

    # Class default so a subclass or test double that skips ``__init__`` still has the attribute.
    _environ: Mapping[str, str] | None = None

    def __init__(self, *, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ
