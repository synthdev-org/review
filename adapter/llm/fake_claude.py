"""A recorded stand-in for the ``claude`` executable, for the Claude CLI backend's tests (plan
Phase 3b): a real subprocess the real adapter spawns, so what the test asserts (argv, stdin,
working directory, environment, process lifecycle) is what the adapter really did.

It is a ``/bin/sh`` script that records every completion call into ``rec/<ns>/{argv,env,cwd,stdin}``
and then behaves per ``mode``. The JSON envelope it prints follows ``--output-format json`` as
documented for Claude Code; ``adapter/llm/test_claude_cli_live.py`` runs the real CLI (opt-in) so
this recording cannot drift from it unnoticed. ``auth status`` calls are counted separately, so a
test can assert that nothing but the login check ran when the backend refused to start.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

_SCRIPT = r"""#!/bin/sh
d="$(dirname "$0")"
mode="$(cat "$d/mode")"
if [ "$1" = "auth" ]; then
  echo x >> "$d/auth_calls"
  if [ "$mode" = "logged_out" ]; then echo '{"loggedIn": false}'; exit 1; fi
  echo '{"loggedIn": true}'; exit 0
fi
c="$d/rec/$(date +%s%N)"
mkdir -p "$c"
printf '%s\0' "$@" > "$c/argv"
env > "$c/env"
pwd > "$c/cwd"
cat > "$c/stdin"
case "$mode" in
  hang) echo $$ > "$d/pid"; exec sleep 30 ;;
  lapsed) echo "Not logged in - Please run /login" >&2; exit 1 ;;
  crash) echo "boom" >&2; exit 3 ;;
  garbage) echo "this is not json" ;;
  huge) exec head -c 5000000 /dev/zero ;;
  *)
    if grep -q "verification assistant" "$c/stdin"; then cat "$d/reply.verify"
    elif grep -q "summary (2-4 sentences)" "$c/stdin"; then cat "$d/reply.summary"
    else cat "$d/reply"; fi ;;
esac
"""


def result_json(text: str, *, is_error: bool = False) -> str:
    """The CLI's ``--output-format json`` result object (extra fields included, as the real one
    has them, so the adapter's tolerance of them is exercised)."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution" if is_error else "success",
            "is_error": is_error,
            "duration_ms": 10,
            "num_turns": 1,
            "result": text,
            "session_id": "00000000-0000-0000-0000-000000000000",
            "total_cost_usd": 0.0,
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }
    )


@dataclass(frozen=True)
class RecordedCall:
    argv: list[str]  # without the executable
    env: dict[str, str]
    cwd: str
    stdin: str


class FakeClaude:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "claude"
        directory.mkdir(parents=True, exist_ok=True)
        self.path.write_text(_SCRIPT)
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)
        self.set_mode("ok")
        self.set_reply("{}")

    def set_mode(self, mode: str) -> None:
        (self.directory / "mode").write_text(mode)

    def set_reply(self, text: str, *, stage: str = "") -> None:
        """What ``ok`` mode answers (``stage`` is ``""``, ``"verify"`` or ``"summary"``)."""
        name = f"reply.{stage}" if stage else "reply"
        (self.directory / name).write_text(result_json(text))

    @property
    def auth_calls(self) -> int:
        marker = self.directory / "auth_calls"
        return len(marker.read_text().splitlines()) if marker.exists() else 0

    @property
    def pid(self) -> int:
        return int((self.directory / "pid").read_text())

    def calls(self) -> list[RecordedCall]:
        recorded = self.directory / "rec"
        if not recorded.exists():
            return []
        calls = []
        for call_dir in sorted(recorded.iterdir()):
            argv = (call_dir / "argv").read_bytes().decode().split("\0")[:-1]
            env = dict(
                line.split("=", 1)
                for line in (call_dir / "env").read_text().splitlines()
                if "=" in line
            )
            calls.append(
                RecordedCall(
                    argv=argv,
                    env=env,
                    cwd=(call_dir / "cwd").read_text().strip(),
                    stdin=(call_dir / "stdin").read_text(),
                )
            )
        return calls


def process_is_gone(pid: int) -> bool:
    """True when ``pid`` no longer exists (reaped; a zombie would still answer signal 0)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False
