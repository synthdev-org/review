"""Helpers for tests that drive ``local_mcp_stub`` (test tooling only, like the stub itself)."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adapter.product_knowledge.mcp_tools import StdioTarget

STUB = Path(__file__).with_name("local_mcp_stub.py")


def stdio_target(
    log: Path, mode: str = "normal", *, keyword: str = "charge", env: dict[str, str] | None = None
) -> StdioTarget:
    """A stdio server that runs the stub with the interpreter running the tests."""
    return StdioTarget(
        sys.executable,
        (str(STUB), "--log", str(log), "--mode", mode, "--keyword", keyword),
        env or {},
    )


def read_log(log: Path) -> list[dict[str, Any]]:
    """Every event the stub wrote (empty if it never ran: nothing was started)."""
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def calls(log: Path) -> list[dict[str, Any]]:
    return [e for e in read_log(log) if e["event"] == "call"]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class HttpStub:
    url: str
    log: Path


@contextmanager
def http_stub(
    log: Path, mode: str = "normal", *, keyword: str = "charge", redirect_to: str | None = None
) -> Iterator[HttpStub]:
    """The stub serving Streamable HTTP on a free loopback port for the length of the block."""
    port = free_port()
    argv = [sys.executable, str(STUB), "--log", str(log), "--mode", mode, "--http", str(port)]
    argv += ["--keyword", keyword]
    if redirect_to is not None:
        argv += ["--redirect-to", redirect_to]
    process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("the HTTP stub did not start")
        yield HttpStub(url=f"http://127.0.0.1:{port}/mcp", log=log)
    finally:
        process.terminate()
        process.wait(timeout=10)
