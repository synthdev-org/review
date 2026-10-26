"""Unit tests for input-side redaction (US-L15), against fakes of the diff source and the scanner.

The real Gitleaks scan is covered by the integration tests in ``adapter/analyzers/test_gitleaks.py``
and ``apps/local_cli/test_trust.py``.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence

import pytest

from adapter.local import redaction
from adapter.local.exceptions import RedactionFailedError
from adapter.local.redaction import RedactingDiffFetcher, is_denied_path
from ingestion.diff import Diff, DiffFile

_SECRET = "SECRETVALUE"
_PATCH = f"@@ -1,4 +1,4 @@ def f():\n context\n-old = '{_SECRET}'\n+new = '{_SECRET}'\n tail"


class FakeFinder:
    """Flags every line containing the secret (the scanner sees patch lines without markers)."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self._fail = fail

    async def secret_lines(self, texts: Sequence[str]) -> list[frozenset[int]]:
        self.calls.append(list(texts))
        if self._fail is not None:
            raise self._fail
        return [
            frozenset(n for n, line in enumerate(t.split("\n"), 1) if _SECRET in line)
            for t in texts
        ]


class FakeDiffSource:
    def __init__(self, files: list[DiffFile]) -> None:
        self._diff = Diff(files=tuple(files))
        self.content_requests: list[list[str]] = []

    async def fetch(self, **_: object) -> Diff:
        return self._diff

    async def fetch_content(
        self, *, diff: Diff, paths: Sequence[str], **_: object
    ) -> dict[str, bytes]:
        self.content_requests.append(list(paths))
        return {p: b"content of " + p.encode() for p in paths}


def _file(path: str, patch: str | None = _PATCH, **extra: str) -> DiffFile:
    return DiffFile(path=path, status="modified", patch=patch, **extra)


async def _fetch(fetcher: RedactingDiffFetcher) -> Diff:
    return await fetcher.fetch(
        installation_id=0, repo_full_name="local/x", pr_number=0, head_sha="h"
    )


def _fetcher(
    files: list[DiffFile],
    finder: FakeFinder | None = None,
    *,
    max_files: int = 10,
    max_bytes: int = 10_000,
) -> tuple[RedactingDiffFetcher, FakeFinder, FakeDiffSource]:
    source, finder = FakeDiffSource(files), finder or FakeFinder()
    return (
        RedactingDiffFetcher(source, finder, max_files=max_files, max_bytes=max_bytes),
        finder,
        source,
    )


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "app/.env.production",
        "config/server.pem",
        "id_rsa",
        "a/b/id_ed25519.pub",
        "x.tfvars",
        "creds/.git-credentials",
        "KEYS/Private.KEY",
        "deploy\\.npmrc",
    ],
)
def test_deny_list_matches_credential_files(path: str) -> None:
    assert is_denied_path(path)


@pytest.mark.parametrize("path", ["app.py", "docs/environment.md", "src/keyboard.py", "README.md"])
def test_deny_list_leaves_ordinary_files_alone(path: str) -> None:
    assert not is_denied_path(path)


@pytest.mark.asyncio
async def test_added_removed_and_context_lines_are_replaced_and_markers_survive() -> None:
    fetcher, _, _ = _fetcher([_file("a.py")])

    diff = await _fetch(fetcher)

    assert diff.files[0].patch == (
        "@@ -1,4 +1,4 @@ def f():\n context\n-[REDACTED]\n+[REDACTED]\n tail"
    )
    assert fetcher.report is not None and fetcher.report.redacted_lines == 2


@pytest.mark.asyncio
async def test_a_secret_in_a_hunk_header_is_replaced_without_breaking_the_header() -> None:
    patch = f"@@ -1,2 +1,2 @@ def {_SECRET}():\n-a\n+b"
    fetcher, _, _ = _fetcher([_file("a.py", patch)])

    diff = await _fetch(fetcher)

    assert diff.files[0].patch == "@@ -1,2 +1,2 @@ [REDACTED]\n-a\n+b"


@pytest.mark.asyncio
async def test_a_secret_in_a_path_is_replaced_and_content_still_maps_back() -> None:
    secret_path = f"dir/{_SECRET}.txt"
    fetcher, _, source = _fetcher([_file(secret_path, previous_path=f"old/{_SECRET}.txt")])

    diff = await _fetch(fetcher)
    content = await fetcher.fetch_content(
        installation_id=0, repo_full_name="local/x", diff=diff, paths=[diff.files[0].path]
    )

    assert _SECRET not in diff.files[0].path and _SECRET not in (diff.files[0].previous_path or "")
    assert source.content_requests == [[secret_path]]
    assert list(content) == [diff.files[0].path]


@pytest.mark.asyncio
async def test_a_deny_listed_file_keeps_its_entry_but_loses_its_patch_and_is_reported() -> None:
    fetcher, finder, _ = _fetcher([_file(".env", "@@ -0,0 +1 @@\n+DEBUG=1"), _file("a.py")])

    diff = await _fetch(fetcher)

    assert diff.files[0].path == ".env" and diff.files[0].patch is None
    assert fetcher.report is not None and fetcher.report.denied_paths == (".env",)
    assert all("DEBUG=1" not in text for text in finder.calls[0])  # not even scanned


@pytest.mark.asyncio
async def test_one_scan_per_run_however_many_steps_fetch() -> None:
    fetcher, finder, _ = _fetcher([_file("a.py"), _file("b.py")])

    first, second = await _fetch(fetcher), await _fetch(fetcher)

    assert first is second
    assert len(finder.calls) == 1  # one batch: the paths and both patches together
    assert len(finder.calls[0]) == 3


@pytest.mark.asyncio
async def test_the_file_count_cap_is_enforced_before_the_scanner_runs() -> None:
    fetcher, finder, _ = _fetcher([_file(f"{i}.py") for i in range(4)], max_files=3)

    with pytest.raises(RedactionFailedError, match="4 files, over the 3-file cap"):
        await _fetch(fetcher)

    assert finder.calls == []


@pytest.mark.asyncio
async def test_the_byte_cap_is_enforced_before_the_scanner_runs() -> None:
    fetcher, finder, _ = _fetcher([_file("a.py", "@@ -1 +1 @@\n+" + "x" * 500)], max_bytes=100)

    with pytest.raises(RedactionFailedError, match="over the 100-byte cap"):
        await _fetch(fetcher)

    assert finder.calls == []


@pytest.mark.asyncio
async def test_a_scanner_failure_fails_closed_without_echoing_anything_from_it() -> None:
    fetcher, _, _ = _fetcher([_file("a.py")], FakeFinder(fail=RuntimeError(f"leaked {_SECRET}")))

    with pytest.raises(RedactionFailedError) as raised:
        await _fetch(fetcher)

    assert "RuntimeError" in raised.value.reason and _SECRET not in str(raised.value)
    assert fetcher.diff is None  # nothing unredacted is ever handed out


@pytest.mark.asyncio
async def test_an_incomplete_scanner_answer_fails_closed() -> None:
    class ShortFinder(FakeFinder):
        async def secret_lines(self, texts: Sequence[str]) -> list[frozenset[int]]:
            return []

    fetcher, _, _ = _fetcher([_file("a.py")], ShortFinder())

    with pytest.raises(RedactionFailedError):
        await _fetch(fetcher)


@pytest.mark.asyncio
async def test_content_is_refused_before_the_diff_was_screened() -> None:
    fetcher, _, _ = _fetcher([_file("a.py")])

    with pytest.raises(RedactionFailedError):
        await fetcher.fetch_content(
            installation_id=0, repo_full_name="local/x", diff=Diff(files=()), paths=["a.py"]
        )


def test_redaction_is_not_inside_the_telemetry_error_suppression_and_has_no_off_switch() -> None:
    source = inspect.getsource(redaction)
    assert "suppress_telemetry_errors(" not in source and "import suppress" not in source
    assert "NoOp" not in source and "enabled" not in source


class MagicByteBlindFinder(FakeFinder):
    """Like gitleaks 8.21: finds nothing in a text whose first bytes look like an archive."""

    async def secret_lines(self, texts: Sequence[str]) -> list[frozenset[int]]:
        found = await super().secret_lines(texts)
        return [
            frozenset() if t.startswith(("PK\x03\x04", "%PDF")) else lines
            for t, lines in zip(texts, found, strict=True)
        ]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("magic", ["PK\x03\x04", "%PDF"])
async def test_a_magic_byte_first_line_cannot_blind_the_scan(magic: str) -> None:
    patch = f"@@ -1,2 +1,2 @@ {magic}x\n-a\n+'{_SECRET}'"
    fetcher, _, _ = _fetcher([_file(f"{magic}.py", patch)], MagicByteBlindFinder())

    diff = await _fetch(fetcher)

    assert diff.files[0].patch == f"@@ -1,2 +1,2 @@ {magic}x\n-a\n+[REDACTED]"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["# gitleaks:allow", "# GitLeaks:Allow"])
async def test_an_inline_allow_marker_cannot_exempt_a_secret_line_from_redaction(
    marker: str,
) -> None:
    from adapter.analyzers.gitleaks import GitleaksSecretLineFinder

    token = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"
    patch = f'@@ -1,1 +1,2 @@\n a = 1\n+github_token = "{token}" {marker}'
    source = FakeDiffSource([_file("a.py", patch)])
    fetcher = RedactingDiffFetcher(
        source,
        GitleaksSecretLineFinder(timeout_seconds=30, ignore_pr_config=True),
        max_files=10,
        max_bytes=10_000,
    )

    diff = await _fetch(fetcher)

    assert diff.files[0].patch == "@@ -1,1 +1,2 @@\n a = 1\n+[REDACTED]"


@pytest.mark.asyncio
async def test_a_deny_listed_files_content_is_never_handed_to_analyzers() -> None:
    fetcher, _, source = _fetcher([_file("config.tfvars"), _file("a.py")])
    diff = await _fetch(fetcher)

    content = await fetcher.fetch_content(
        installation_id=0,
        repo_full_name="local/x",
        diff=diff,
        paths=["config.tfvars", "a.py"],
    )

    assert list(content) == ["a.py"]
    assert source.content_requests == [["a.py"]]


@pytest.mark.asyncio
async def test_content_of_a_file_renamed_away_from_a_denied_name_is_withheld() -> None:
    renamed = DiffFile(path="notes.txt", status="renamed", patch=_PATCH, previous_path=".env")
    fetcher, _, source = _fetcher([renamed])

    diff = await _fetch(fetcher)
    content = await fetcher.fetch_content(
        installation_id=0, repo_full_name="local/x", diff=diff, paths=["notes.txt"]
    )

    assert diff.files[0].patch is None
    assert content == {} and source.content_requests == [[]]
