"""Structured JSON logging setup for the admin_api entrypoint.

Configured independently here — its own small copy, matching
``apps/webhook_api/logging_config.py``'s and ``apps/worker``'s own copies
byte-for-byte apart from this docstring — rather than factored into a
shared/common/utils package. See docs/rules/architecture.md's "no
common/shared/utils dumps" rule: each entrypoint intentionally owns a
small, identical copy of this setup rather than share a package solely for
it.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Attributes stdlib `logging.LogRecord` always carries. Anything else found
# on a record (passed via `logger.info(..., extra={...})`) is treated as a
# caller-supplied structured field and folded into the JSON payload.
_STANDARD_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys())


class JSONFormatter(logging.Formatter):
    """Renders each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and key != "message":
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    """Configure the root logger for structured JSON output at the given level.

    Idempotent: safe to call once at process start from this entrypoint's
    composition root (main.py). Replaces any handlers already attached to
    the root logger rather than stacking duplicates on repeated calls (e.g.
    across test runs that re-import this module).
    """
    root = logging.getLogger()
    root.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
