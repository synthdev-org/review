"""Integration tests for adapter.embedding.startup_check
(NFR — embedding model startup check), per spec-indexing.md's Non-Functional
Requirements:
"An unloadable local embedding model fails Worker startup fast with a
clear error, mirroring `pgvector_check.py`'s pattern; a loadable model
starts cleanly."

Real local model, no external network, per docs/rules/testing.md's adapter
convention — see adapter/embedding/test_local_onnx.py's own module
docstring for why `/app/models/all-MiniLM-L6-v2` is real inside this
repo's `dev` image, not a placeholder.
"""

from __future__ import annotations

import os

import pytest

from adapter.embedding.local_onnx import LocalONNXEmbeddingClient
from adapter.embedding.startup_check import check_embedding_model_loaded
from indexing.embedding import EmbeddingError

pytestmark = pytest.mark.integration

_REAL_MODEL_PATH = "/app/models/all-MiniLM-L6-v2"
_CORRUPTED_MODEL_PATH = os.path.join(os.path.dirname(__file__), "testdata", "corrupted_model")


def test_check_passes_when_model_loads_and_infers_successfully() -> None:
    """A loadable model starts cleanly — the real bundled model, baked
    into this repo's `dev`/`runtime` images, must not raise."""
    client = LocalONNXEmbeddingClient(model_path=_REAL_MODEL_PATH)

    check_embedding_model_loaded(client)  # must not raise


def test_check_raises_typed_error_when_model_is_unloadable() -> None:
    """An unloadable local embedding model fails fast with a clear,
    typed error — never a raw onnxruntime/fastembed exception, mirroring
    `adapter/storage/test_pgvector_presence.py`'s own
    `test_check_raises_typed_error_when_extension_missing` shape."""
    client = LocalONNXEmbeddingClient(model_path=_CORRUPTED_MODEL_PATH)

    with pytest.raises(EmbeddingError):
        check_embedding_model_loaded(client)


def test_check_raises_typed_error_when_model_path_does_not_exist() -> None:
    """A missing model directory entirely (e.g. a broken Dockerfile bake,
    or a misconfigured `embedding_model_path`) fails the same way as a
    corrupted one — a typed error, not a raw exception, and not a silent
    pass."""
    client = LocalONNXEmbeddingClient(model_path="/nonexistent/model/path")

    with pytest.raises(EmbeddingError):
        check_embedding_model_loaded(client)
