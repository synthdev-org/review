"""Unit tests for the repository memory file: the bounded parser, fingerprint hashing, the matcher
and the proposal text. Reading from git (CI) is covered by the integration tests in
``apps/local_cli/test_memory_file.py``."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from adapter.local import memory_file as mf
from adapter.local.conftest import init_repo, requires_git
from adapter.local.exceptions import GitCommandError, MemoryFileError
from adapter.local.memory_file import (
    PROPOSAL_MIN_DISMISSALS,
    FileMemoryMatcher,
    LoadedMemoryFile,
    MemoryEntry,
    describe_suppressed,
    fingerprint_digest,
    load_memory_file,
    parse_memory_file,
    propose_rules,
    render_proposal,
)
from memory.fingerprint import build_finding_fingerprint


def _line(**fields: object) -> str:
    return json.dumps({"file": "a.py", "message": "m", **fields})


def _parse(text: str) -> LoadedMemoryFile:
    return parse_memory_file(text.encode(), path=".review/memory.jsonl")


# --- parser -----------------------------------------------------------------------------------


def test_parses_entries_and_ignores_blank_lines_and_comments() -> None:
    loaded = _parse(f"# why\n\n{_line(rule_id='E501', note='ok')}\n  {_line(file='b.py')}  \n")

    assert [(e.file, e.rule_id, e.note) for e in loaded.entries] == [
        ("a.py", "E501", "ok"),
        ("b.py", None, None),
    ]


@pytest.mark.parametrize(
    ("text", "line", "fragment"),
    [
        ("# c\n{not json}\n", 2, "not valid JSON"),
        (f"{_line()}\n\n[1]\n", 3, "must be a JSON object"),
        (f"{_line()}\n{_line(extra=1)}\n", 2, "unknown field"),
        ('{"file": "a.py"}\n', 1, "message"),
        ('{"file": "", "message": "m"}\n', 1, "file"),
        ('{"file": "a.py", "message": 5}\n', 1, "message"),
        ("[" * 4000 + "\n", 1, "JSON"),
    ],
)
def test_a_malformed_line_is_an_error_naming_the_line(text: str, line: int, fragment: str) -> None:
    with pytest.raises(MemoryFileError) as caught:
        _parse(text)

    assert caught.value.line == line
    assert f".review/memory.jsonl:{line}:" in str(caught.value)
    assert fragment in str(caught.value)


def test_an_error_never_quotes_the_offending_content() -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    with pytest.raises(MemoryFileError) as caught:
        _parse(f'{{"file": "a.py", "message": "m", "note": 1, "{secret}": 2}}\n')

    assert secret not in str(caught.value)


def test_invalid_utf8_names_the_line() -> None:
    with pytest.raises(MemoryFileError) as caught:
        parse_memory_file(_line().encode() + b"\n\xff\xfe\n", path="m")

    assert caught.value.line == 2


def test_limits_are_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mf, "MEMORY_FILE_MAX_BYTES", 50)
    with pytest.raises(MemoryFileError, match="byte limit"):
        _parse(_line() + "\n" + _line())
    monkeypatch.setattr(mf, "MEMORY_FILE_MAX_BYTES", 10_000)
    monkeypatch.setattr(mf, "MEMORY_FILE_MAX_LINES", 3)
    with pytest.raises(MemoryFileError, match="line limit"):
        _parse("\n" * 5)
    monkeypatch.setattr(mf, "MEMORY_FILE_MAX_LINES", 100)
    monkeypatch.setattr(mf, "MEMORY_FILE_MAX_LINE_CHARS", 20)
    with pytest.raises(MemoryFileError) as caught:
        _parse("# fine\n" + _line() + "\n")
    assert caught.value.line == 2


def test_a_parse_that_runs_out_of_time_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mf, "PARSE_TIMEOUT_SECONDS", -1.0)

    with pytest.raises(MemoryFileError, match="took too long"):
        _parse(_line() + "\n")


def test_a_file_of_only_comments_is_loaded_with_no_entries() -> None:
    assert _parse("# nothing dismissed yet\n").entries == ()


# --- fingerprint / matcher ----------------------------------------------------------------------


def test_the_fingerprint_digest_is_a_stable_sha256_of_the_shared_fingerprint_text() -> None:
    text = build_finding_fingerprint(file="a.py", rule_id="E501", message="too long")

    assert text == "a.py\nE501\ntoo long"  # the text server mode embeds
    assert fingerprint_digest(text) == hashlib.sha256(text.encode()).hexdigest()
    assert fingerprint_digest(text) == fingerprint_digest(text)
    assert fingerprint_digest(text) != fingerprint_digest(text + " ")


@pytest.mark.asyncio
async def test_the_matcher_dismisses_only_an_exact_fingerprint_and_records_what_it_dismissed() -> (
    None
):
    matcher = FileMemoryMatcher((MemoryEntry(file="a.py", rule_id="E501", message="too long"),))
    hit = build_finding_fingerprint(file="a.py", rule_id="E501", message="too long")

    assert await matcher.is_dismissed(repo_id="r", fingerprint=hit)
    for miss in (
        build_finding_fingerprint(file="b.py", rule_id="E501", message="too long"),
        build_finding_fingerprint(file="a.py", rule_id=None, message="too long"),
        build_finding_fingerprint(file="a.py", rule_id="E501", message="too long."),
        "garbage",
    ):
        assert not await matcher.is_dismissed(repo_id="r", fingerprint=miss)
    assert [(s.file, s.rule_id, s.message) for s in matcher.suppressed] == [
        ("a.py", "E501", "too long")
    ]


@pytest.mark.asyncio
async def test_a_wildcard_entry_dismisses_the_rule_and_message_in_any_file() -> None:
    matcher = FileMemoryMatcher((MemoryEntry(file="*", rule_id=None, message="flaky"),))

    assert await matcher.is_dismissed(
        repo_id="r",
        fingerprint=build_finding_fingerprint(file="x/y.py", rule_id=None, message="flaky"),
    )
    assert not await matcher.is_dismissed(
        repo_id="r",
        fingerprint=build_finding_fingerprint(file="x/y.py", rule_id="R1", message="flaky"),
    )


@pytest.mark.asyncio
async def test_a_path_containing_a_newline_is_never_dismissed() -> None:
    matcher = FileMemoryMatcher((MemoryEntry(file="*", rule_id=None, message="flaky"),))
    fingerprint = build_finding_fingerprint(file="a\nb.py", rule_id=None, message="flaky")

    assert not await matcher.is_dismissed(repo_id="r", fingerprint=fingerprint)
    assert matcher.suppressed == []


def test_suppressed_findings_are_described_without_control_characters() -> None:
    matcher = FileMemoryMatcher(())
    matcher.suppressed.append(
        mf.SuppressedFinding(file="a.py", rule_id="R\x1b[31m", message="m\nx")
    )

    [line] = describe_suppressed(matcher.suppressed)

    assert "\x1b" not in line and "\n" not in line
    assert line.startswith("a.py: m")


# --- proposals ----------------------------------------------------------------------------------


def _dismissed_in(
    count: int, *, rule_id: str | None = "R1", message: str = "flaky"
) -> tuple[MemoryEntry, ...]:
    return tuple(
        MemoryEntry(file=f"f{i}.py", rule_id=rule_id, message=message) for i in range(count)
    )


def test_a_rule_is_proposed_at_the_documented_threshold_and_not_below_it() -> None:
    assert PROPOSAL_MIN_DISMISSALS == 3
    assert propose_rules(_dismissed_in(PROPOSAL_MIN_DISMISSALS - 1)) == ()

    [proposal] = propose_rules(_dismissed_in(PROPOSAL_MIN_DISMISSALS))

    assert (proposal.file, proposal.rule_id, proposal.message) == ("*", "R1", "flaky")


def test_the_same_file_dismissed_repeatedly_counts_once() -> None:
    entries = tuple(MemoryEntry(file="a.py", rule_id="R1", message="flaky") for _ in range(5))

    assert propose_rules(entries) == ()


def test_nothing_is_proposed_when_a_wildcard_entry_already_exists() -> None:
    entries = (*_dismissed_in(4), MemoryEntry(file="*", rule_id="R1", message="flaky"))

    assert propose_rules(entries) == ()


def test_different_messages_are_not_pooled() -> None:
    entries = (*_dismissed_in(2, message="a"), *_dismissed_in(2, message="b"))

    assert propose_rules(entries) == ()


def test_the_proposal_is_a_unified_diff_that_only_appends() -> None:
    text = "\n".join(e.model_dump_json(exclude_none=True) for e in _dismissed_in(3)) + "\n"
    loaded = parse_memory_file(text.encode(), path=".review/memory.jsonl")

    diff = render_proposal(loaded, propose_rules(loaded.entries))

    lines = diff.splitlines()
    assert lines[0] == "--- a/.review/memory.jsonl" and lines[1] == "+++ b/.review/memory.jsonl"
    assert [line for line in lines if line.startswith("-") and not line.startswith("---")] == []
    [added] = [line for line in lines if line.startswith("+") and not line.startswith("+++")]
    assert json.loads(added[1:])["file"] == "*"


def test_no_proposal_means_no_diff() -> None:
    assert render_proposal(_parse(_line() + "\n"), ()) == ""


# --- loading from the working tree ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_missing_file_is_none_and_a_present_one_is_loaded(tmp_path: Path) -> None:
    assert (
        await load_memory_file(
            tmp_path, ".review/memory.jsonl", base_commit=None, environ=os.environ
        )
        is None
    )
    (tmp_path / ".review").mkdir()
    (tmp_path / ".review" / "memory.jsonl").write_text(_line() + "\n")

    loaded = await load_memory_file(
        tmp_path, ".review/memory.jsonl", base_commit=None, environ=os.environ
    )

    assert loaded is not None and len(loaded.entries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/etc/passwd", "../memory.jsonl", "a/../../m", "", "a\\b"])
async def test_a_path_outside_the_repository_is_refused(tmp_path: Path, path: str) -> None:
    with pytest.raises(MemoryFileError, match="relative"):
        await load_memory_file(tmp_path, path, base_commit=None, environ=os.environ)


@pytest.mark.asyncio
async def test_a_symlinked_file_or_directory_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text(_line() + "\n")
    repo = tmp_path / "repo"
    (repo / "d").mkdir(parents=True)
    (repo / "link.jsonl").symlink_to(outside)
    (repo / "dirlink").symlink_to(repo / "d")
    (repo / "d" / "m.jsonl").write_text(_line() + "\n")

    for path in ("link.jsonl", "dirlink/m.jsonl"):
        with pytest.raises(MemoryFileError, match="symbolic link"):
            await load_memory_file(repo, path, base_commit=None, environ=os.environ)


@pytest.mark.asyncio
async def test_an_oversized_or_non_regular_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "m.jsonl").write_text(_line() + "\n")
    monkeypatch.setattr(mf, "MEMORY_FILE_MAX_BYTES", 5)
    with pytest.raises(MemoryFileError, match="byte limit"):
        await load_memory_file(tmp_path, "m.jsonl", base_commit=None, environ=os.environ)
    (tmp_path / "dir").mkdir()
    with pytest.raises(MemoryFileError, match="regular file"):
        await load_memory_file(tmp_path, "dir", base_commit=None, environ=os.environ)


# --- loading from a commit (CI): real git, never skipped when git is installed -------------------


@pytest.mark.asyncio
@requires_git
@pytest.mark.integration
async def test_read_from_commit_returns_the_blob_at_that_commit_not_the_working_tree(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write(".review/memory.jsonl", _line(message="at base") + "\n")
    base = repo.commit("base")
    repo.write(".review/memory.jsonl", _line(message="at head") + "\n")  # the PR rewrites it
    repo.commit("head")
    repo.write(".review/memory.jsonl", _line(message="uncommitted") + "\n")

    loaded = await load_memory_file(
        repo.path, ".review/memory.jsonl", base_commit=base, environ=os.environ
    )

    assert loaded is not None
    assert [entry.message for entry in loaded.entries] == ["at base"]


@pytest.mark.asyncio
@requires_git
@pytest.mark.integration
async def test_read_from_commit_is_none_when_the_commit_has_no_such_file(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write("README.md", "x\n")
    base = repo.commit("base")
    repo.write(".review/memory.jsonl", _line() + "\n")  # added only after the base
    repo.commit("head")

    assert (
        await load_memory_file(
            repo.path, ".review/memory.jsonl", base_commit=base, environ=os.environ
        )
        is None
    )


@pytest.mark.asyncio
@requires_git
@pytest.mark.integration
async def test_read_from_commit_refuses_a_symlink_and_an_oversized_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo.path / "target.jsonl").write_text(_line() + "\n")
    (repo.path / "link.jsonl").symlink_to("target.jsonl")
    repo.write("big.jsonl", _line() + "\n")
    commit = repo.commit("base")

    with pytest.raises(MemoryFileError, match="not a regular file on the base branch"):
        await load_memory_file(repo.path, "link.jsonl", base_commit=commit, environ=os.environ)
    monkeypatch.setattr(mf, "MEMORY_FILE_MAX_BYTES", 5)
    with pytest.raises(MemoryFileError, match="byte limit"):
        await load_memory_file(repo.path, "big.jsonl", base_commit=commit, environ=os.environ)


@pytest.mark.asyncio
@requires_git
@pytest.mark.integration
async def test_read_from_commit_reports_an_unknown_commit_as_a_git_error(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write("a", "x\n")
    repo.commit("base")

    with pytest.raises(GitCommandError):
        await load_memory_file(repo.path, "m.jsonl", base_commit="f" * 40, environ=os.environ)


@pytest.mark.asyncio
async def test_an_entry_copied_from_a_truncated_ci_comment_still_dismisses_the_full_message() -> (
    None
):
    from findings.markdown import MESSAGE_CAP, sanitize_message

    full = "x" * (MESSAGE_CAP + 500)
    shown = sanitize_message(full)
    matcher = FileMemoryMatcher((MemoryEntry(file="a.py", rule_id=None, message=shown),))

    assert shown != full
    assert await matcher.is_dismissed(
        repo_id="r", fingerprint=build_finding_fingerprint(file="a.py", rule_id=None, message=full)
    )
