"""indexing.prefilter — the indexing job's pre-filter (US9).

Narrows a pushed file-path list down to the subset that should reach the
chunker: only files matching an in-scope extension
(``.ts``/``.tsx``/``.php``/``.go``, per ``indexing.chunk.language_for_path``)
and not matching an ignore pattern proceed.

**Duplicated, not imported, ignore-pattern constants:**
``review/prefilter.py``'s lockfile/vendored-directory/
generated-file/binary-extension constants are copied here verbatim rather
than imported across packages. Importing ``review`` package internals from
``indexing`` would cross a bounded-context boundary
``docs/rules/architecture.md``'s package-structure rule argues against, and
moving the constants to a new shared location would recreate the
``common``/``shared``/``utils`` dump that same rule explicitly forbids.
Four small ``frozenset``/tuple constants duplicated across two files is a
cheap, explicit trade-off, not a shared-module workaround. Keep any future
edit to one copy in sync with the other by hand — there is deliberately no
single source of truth for these.

Pure, no I/O — this module never fetches anything itself; the indexing
pipeline calls it against the file-path list already resolved by that
job's diff/tree-fetch step, before any file content is fetched or chunked.

**No per-file size ceiling beyond the aggregate ceilings — an accepted
limitation:** ``review/prefilter.py``
has the identical shape of gap today (aggregate file-count/byte ceilings
only, no per-file cap); this is inherited, symmetric, existing-precedent
behavior. The aggregate ceilings themselves (US6/US8's 500-file/200MB
incremental and 20,000-file/2GB full-run defaults) are enforced by the
indexing pipeline, not here — this module's own job is purely the per-path
extension/ignore-pattern decision.
"""

from __future__ import annotations

import fnmatch
import posixpath
from collections.abc import Sequence

from indexing.chunk import language_for_path

# Lockfiles: matched by basename, regardless of directory depth.
# Duplicated from review/prefilter.py — see this module's own docstring.
_IGNORED_LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Pipfile.lock",
        "poetry.lock",
        "uv.lock",
        "Gemfile.lock",
        "go.sum",
        "Cargo.lock",
        "composer.lock",
        "mix.lock",
    }
)

# Vendored paths: matched as a path segment, anywhere in the path.
# Duplicated from review/prefilter.py — see this module's own docstring.
_IGNORED_DIRECTORY_SEGMENTS = frozenset(
    {
        "vendor",
        "node_modules",
        ".venv",
        "venv",
        "third_party",
        "dist",
        "build",
    }
)

# Generated files: matched via fnmatch glob against the full path.
# Duplicated from review/prefilter.py — see this module's own docstring.
_IGNORED_GENERATED_GLOBS = (
    "*.min.js",
    "*.min.css",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.pb.go",
    "*.generated.*",
)

# Binary files: extension-based, not content-sniffed.
# Duplicated from review/prefilter.py — see this module's own docstring.
_IGNORED_BINARY_EXTENSION_GLOBS = (
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.webp",
    "*.pdf",
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.gz",
    "*.7z",
    "*.exe",
    "*.dll",
    "*.dylib",
    "*.so",
    "*.bin",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.eot",
    "*.class",
    "*.jar",
    "*.wasm",
    "*.mp4",
    "*.mov",
    "*.mp3",
)


def _is_ignored(path: str) -> bool:
    """Whether ``path`` matches one of this module's default ignore-pattern
    categories (lockfile, vendored path, generated file, binary file) —
    duplicated logic from ``review.prefilter._is_ignored``, see this
    module's own docstring for why.
    """
    basename = posixpath.basename(path)
    if basename in _IGNORED_LOCKFILE_NAMES:
        return True
    if any(segment in _IGNORED_DIRECTORY_SEGMENTS for segment in path.split("/")):
        return True
    if any(fnmatch.fnmatch(path, pattern) for pattern in _IGNORED_GENERATED_GLOBS):
        return True
    return any(fnmatch.fnmatch(path, pattern) for pattern in _IGNORED_BINARY_EXTENSION_GLOBS)


def is_indexable_path(path: str) -> bool:
    """Whether ``path`` should reach the chunker at all — an in-scope
    extension (per ``indexing.chunk.language_for_path``) and not matching
    any ignore pattern, per US9's own Given/When/Then.
    """
    if _is_ignored(path):
        return False
    return language_for_path(path) is not None


def filter_indexable_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """Convenience bulk form of ``is_indexable_path``, preserving ``paths``'
    own relative order — the shape the indexing pipeline actually consumes
    (a job's whole added/modified path list at once).
    """
    return tuple(path for path in paths if is_indexable_path(path))
