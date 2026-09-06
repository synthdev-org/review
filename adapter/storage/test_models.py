"""Unit test: the embedding-vector dimension constant, deliberately
duplicated across ``adapter/storage/models.py``, ``adapter/embedding/
local_onnx.py``, and the ``0003_indexing_schema`` migration (each module's
own docstring/comment explains why it isn't imported across that
boundary), never silently drifts out of sync. No database required — pure
import-and-compare.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from adapter.embedding.local_onnx import EMBEDDING_DIMENSION
from adapter.storage import models

_MIGRATION_0003_PATH = Path(__file__).parent / "migrations" / "versions" / "0003_indexing_schema.py"


def _load_migration_0003() -> object:
    # The file's own name isn't a valid Python module identifier (leading
    # digit), the same reason Alembic itself loads revision files by path
    # rather than by ``import`` statement.
    spec = importlib.util.spec_from_file_location(
        "_migration_0003_indexing_schema", _MIGRATION_0003_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_embedding_dimension_constant_is_consistent_everywhere() -> None:
    migration_0003 = _load_migration_0003()
    assert models._EMBEDDING_DIMENSION == EMBEDDING_DIMENSION
    assert migration_0003._EMBEDDING_DIMENSION == EMBEDDING_DIMENSION  # type: ignore[attr-defined]
