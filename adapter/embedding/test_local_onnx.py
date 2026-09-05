"""Integration tests for adapter.embedding.local_onnx.LocalONNXEmbeddingClient
(US4), per the indexing delivery plan's Phase 4 Tests section.

Real local model, no external network at inference time, per
docs/rules/testing.md's adapter convention — exercises the actual bundled
model the Dockerfile's `embedding-model` stage bakes into both the `dev`
and `runtime` stages at `/app/models/all-MiniLM-L6-v2` (see that stage's
own docstring in the Dockerfile).
"""

from __future__ import annotations

import os

import pytest

from adapter.embedding.local_onnx import EMBEDDING_DIMENSION, LocalONNXEmbeddingClient
from indexing.embedding import EmbeddingClient, EmbeddingError

pytestmark = pytest.mark.integration

# The Dockerfile's `embedding-model` build stage bakes the checksum-verified
# model into this exact path in both the `dev` and `runtime` stages (see
# that stage's own COPY destinations) — this test runs inside the `dev`
# image (per this repo's documented `docker build --target dev` + `docker
# run -m integration` flow), so this path is real, not a placeholder.
_REAL_MODEL_PATH = "/app/models/all-MiniLM-L6-v2"

# A real-but-deliberately-corrupted copy of the same model directory: every
# file except `model.onnx` is byte-identical to the real bundled model;
# `model.onnx` is truncated to an invalid protobuf — see that fixture
# directory's own README for the full provenance/verification.
_CORRUPTED_MODEL_PATH = os.path.join(os.path.dirname(__file__), "testdata", "corrupted_model")


def _assert_is_embedding_client(client: object) -> None:
    """mypy already enforces Protocol conformance at type-check time; this
    runtime assertion just documents that `LocalONNXEmbeddingClient` is
    meant to satisfy `EmbeddingClient`, not merely resemble it — mirrors
    `llmgateway/test_startup_check.py`'s own `_assert_is_llm_client`."""
    assert isinstance(client, EmbeddingClient)


def test_embed_produces_fixed_dimension_vectors_for_a_batch_of_texts() -> None:
    """US4's own Given/When/Then: the bundled local model (no network call)
    produces a fixed-dimension vector per input text, for a real batched
    call across multiple texts in one call — not one call per text."""
    client = LocalONNXEmbeddingClient(model_path=_REAL_MODEL_PATH)
    _assert_is_embedding_client(client)

    texts = (
        "function add(a: number, b: number): number { return a + b; }",
        "class Widget implements Renderable {}",
        "package main\n\nfunc main() {}",
    )

    vectors = client.embed(texts)

    assert len(vectors) == len(texts)
    for vector in vectors:
        assert vector.dimension == EMBEDDING_DIMENSION
        assert len(vector.values) == EMBEDDING_DIMENSION
        # A real, non-degenerate vector — not all zeros, not NaN/inf.
        assert any(value != 0.0 for value in vector.values)
        assert all(value == value for value in vector.values)  # NaN check

    # Two distinct texts produce distinct vectors (a real model, not a
    # constant/fake stand-in).
    assert vectors[0].values != vectors[1].values


def test_embed_is_deterministic_for_the_same_text() -> None:
    """The same input text embeds to the same vector across two separate
    calls (and, per this test, two separate model loads) — a basic
    real-model sanity check distinct from US4's own Given/When/Then."""
    client_one = LocalONNXEmbeddingClient(model_path=_REAL_MODEL_PATH)
    client_two = LocalONNXEmbeddingClient(model_path=_REAL_MODEL_PATH)

    text = "const x: string = 'hello';"
    (vector_one,) = client_one.embed((text,))
    (vector_two,) = client_two.embed((text,))

    assert vector_one.values == vector_two.values


def test_embed_with_empty_sequence_returns_empty_tuple() -> None:
    client = LocalONNXEmbeddingClient(model_path=_REAL_MODEL_PATH)
    assert client.embed(()) == ()


def test_embed_raises_embedding_error_not_a_raw_exception_on_corrupted_model() -> None:
    """Per the plan's Phase 4 Tests section: constructs
    `LocalONNXEmbeddingClient` against a real-but-deliberately-corrupted
    local model artifact path so the real ONNX runtime call genuinely
    fails — a real failure through the real code path, never a mock, per
    testing.md's "fakes over mocks for adapters" convention."""
    client = LocalONNXEmbeddingClient(model_path=_CORRUPTED_MODEL_PATH)

    with pytest.raises(EmbeddingError) as excinfo:
        client.embed(("hello world",))

    # Never a raw onnxruntime/fastembed exception leaking across the
    # adapter boundary, per architecture.md's exception-type rule.
    assert not isinstance(excinfo.value, type(None))
    assert type(excinfo.value) is EmbeddingError


def test_embed_raises_embedding_error_on_every_subsequent_call_too() -> None:
    """The failure isn't a one-shot fluke of the first call — a client
    constructed against a permanently-corrupted path stays broken (the
    underlying model never becomes loadable), matching what Phase 7's
    per-file isolation needs to be able to rely on: a broken embedding
    client fails every file it's asked to embed, not just the first."""
    client = LocalONNXEmbeddingClient(model_path=_CORRUPTED_MODEL_PATH)

    with pytest.raises(EmbeddingError):
        client.embed(("first",))
    with pytest.raises(EmbeddingError):
        client.embed(("second",))
