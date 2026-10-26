"""Unit tests for the pure parsing in adapter.local.diff (no git, no I/O)."""

from __future__ import annotations

import pytest

from adapter.local.diff import RawEntry, parse_raw_diff, split_patches
from ingestion.diff import DiffContentMalformedError

_A, _B, _C, _D, _ZERO = "a" * 40, "b" * 40, "c" * 40, "d" * 40, "0" * 40


def _record(old_mode: str, new_mode: str, old: str, new: str, status: str) -> bytes:
    return f":{old_mode} {new_mode} {old} {new} {status}".encode()


def test_parse_raw_diff_reads_status_mode_sha_and_paths() -> None:
    raw = b"\0".join(
        [
            _record("100644", "100644", _A, _B, "M"),
            b"src/app.py",
            _record("000000", "100755", _ZERO, _C, "A"),
            b"run.sh",
            _record("100644", "000000", _D, _ZERO, "D"),
            b"old.py",
            _record("100644", "100644", _A, _B, "R087"),
            b"before.py",
            b"after.py",
            b"",
        ]
    )

    assert parse_raw_diff(raw, repo_full_name="local/x") == [
        RawEntry("modified", "100644", _B, "src/app.py", None),
        RawEntry("added", "100755", _C, "run.sh", None),
        RawEntry("removed", "000000", _ZERO, "old.py", None),
        RawEntry("renamed", "100644", _B, "after.py", "before.py"),
    ]


def test_parse_raw_diff_keeps_unusual_path_characters_intact() -> None:
    raw = _record("100644", "100644", _A, _B, "M") + b"\0dir with space/na\xc3\xafve\ttab.py\0"

    [entry] = parse_raw_diff(raw, repo_full_name="local/x")

    assert entry.path == "dir with space/naïve\ttab.py"


def test_parse_raw_diff_of_nothing_is_empty() -> None:
    assert parse_raw_diff(b"", repo_full_name="local/x") == []


@pytest.mark.parametrize(
    "raw",
    [
        b"not a record\0path\0",
        _record("100644", "100644", _A, _B, "M"),  # the path is missing
        _record("100644", "100644", _A, _B, "R100") + b"\0only-one-path\0",
        _record("100644", "100644", _A, _B, "X") + b"\0path\0",  # unknown status letter
    ],
)
def test_parse_raw_diff_rejects_malformed_records(raw: bytes) -> None:
    with pytest.raises(DiffContentMalformedError):
        parse_raw_diff(raw, repo_full_name="local/x")


_TWO_FILES = """\
diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 def total(items):
-    return sum(items)
+    result = 0
+    return result
\\ No newline at end of file
diff --git a/logo.png b/logo.png
index 3333333..4444444 100644
Binary files a/logo.png and b/logo.png differ
diff --git a/old.py b/new.py
similarity index 100%
rename from old.py
rename to new.py
diff --git a/run.sh b/run.sh
old mode 100644
new mode 100755
diff --git a/notes.txt b/notes.txt
index 5555555..6666666 100644
--- a/notes.txt
+++ b/notes.txt
@@ -1 +1 @@
-diff --git is just text in a removed line
+and so is this
"""


def test_split_patches_returns_hunks_only_one_per_file_in_order() -> None:
    patches = split_patches(_TWO_FILES)

    assert len(patches) == 5
    assert patches[0] is not None
    assert patches[0].startswith("@@ -1,2 +1,3 @@\n")
    assert "diff --git" not in patches[0]
    assert patches[0].endswith("\\ No newline at end of file")
    # No hunks: binary, pure rename, mode-only change.
    assert patches[1] is None
    assert patches[2] is None
    assert patches[3] is None
    # A body line that mentions `diff --git` is prefixed, so it does not start a new file.
    assert patches[4] == "@@ -1 +1 @@\n-diff --git is just text in a removed line\n+and so is this"


def test_split_patches_of_empty_output_is_empty() -> None:
    assert split_patches("") == []
