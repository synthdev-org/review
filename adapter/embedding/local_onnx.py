"""adapter.embedding.local_onnx — ``fastembed``-backed implementation of
``indexing.embedding.EmbeddingClient`` (US4). ``fastembed`` is used rather than a
lower-level ``onnxruntime``+``tokenizers`` pairing or
``sentence-transformers``.

Runs ``sentence-transformers/all-MiniLM-L6-v2`` in-process, entirely from
the model weights baked into this image at build time (see the
Dockerfile's ``embedding-model`` stage) — **no network call at inference
time, and none at construction time either**: ``specific_model_path`` (see
below) points ``fastembed`` at that already-on-disk directory instead of
letting it attempt its own download/cache-population, which is exactly the
"bundled local model, never BYO/no-network" story ``spec-indexing.md``'s
Privacy/Compliance section commits to.

**Model naming, stated precisely (a deliberate choice of fastembed's
registry name over the ``Xenova/all-MiniLM-L6-v2`` name):**
``fastembed``'s own model registry (``fastembed.text.pooled_normalized_embedding``)
ships this exact model — same architecture, same weights family, same
384-dim output — under the canonical name
``sentence-transformers/all-MiniLM-L6-v2``, sourced from the
``Qdrant/all-MiniLM-L6-v2-onnx`` Hugging Face repository (an ONNX
conversion of the same upstream ``sentence-transformers/all-MiniLM-L6-v2``
weights ``Xenova/all-MiniLM-L6-v2`` also ONNX-converts independently) —
verified directly against ``fastembed``'s own source before implementation,
not assumed. Using ``fastembed``'s own registry name/source is what makes
``specific_model_path`` (a plain local-directory override, no download
logic of its own to work around) actually work; a re-hosted/renamed copy of
a different ONNX export would not. The Dockerfile's checksum pinning (see
below) is what actually matters for the provenance guarantee,
independent of which upstream export mirror the bytes originated from.

**Why the model isn't loaded eagerly in ``__init__``:** the Dockerfile
bakes weights into every image regardless of whether the process that
imports this module ever actually calls ``embed`` (the Webhook API image
shares the same ``runtime`` stage but never constructs this class) —
loading a ~90MB ONNX model into memory as an import-time or
construction-time side effect would be wasted work for that process, and
would also make unit-testing *construction* of this class (argument
plumbing, not real inference) require the real model files. The model is
therefore loaded lazily, once, on first ``embed`` call (or the startup
check's own first call, per ``adapter/embedding/startup_check.py``) and
cached for the client's lifetime.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastembed import TextEmbedding

from indexing.embedding import EmbeddingClient, EmbeddingError, EmbeddingVector

# The exact registry name fastembed ships this model family under — see
# this module's own docstring for why this isn't the literal
# `Xenova/all-MiniLM-L6-v2` string. Fixed, not configurable: swapping the
# model is a code change (a different `EMBEDDING_DIMENSION`, a different
# Dockerfile pin, and a schema migration for `index_chunk.embedding`'s
# `Vector(384)` column), never a runtime setting.
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMENSION = 384


class LocalONNXEmbeddingClient:
    """`indexing.embedding.EmbeddingClient` implementation backed by a
    local, in-process ONNX model (`fastembed`).

    `model_path` is the on-disk directory the Dockerfile bakes the
    checksum-verified model weights/tokenizer files into (see the
    Dockerfile's `embedding-model` stage and `apps/worker/settings.py`'s
    `embedding_model_path` field) — passed straight through to
    `fastembed.TextEmbedding`'s own `specific_model_path`, which returns
    that path unchanged rather than resolving/downloading anything, per
    `fastembed.common.model_management`'s own `download_model` (verified
    directly against that source before implementation).
    """

    def __init__(self, *, model_path: str, threads: int | None = None) -> None:
        self._model_path = model_path
        self._threads = threads
        self._model: TextEmbedding | None = None

    def _loaded_model(self) -> TextEmbedding:
        """Construct the underlying `fastembed.TextEmbedding` on first use
        and cache it — see this module's own docstring for why this isn't
        done in `__init__`. Never raises a raw `fastembed`/ONNX-runtime
        exception; translated to `EmbeddingError` per architecture.md's
        exception-type rule.
        """
        if self._model is not None:
            return self._model

        try:
            model = TextEmbedding(
                model_name=MODEL_NAME,
                specific_model_path=self._model_path,
                threads=self._threads,
            )
        except Exception as exc:
            raise EmbeddingError(
                detail=(
                    f"could not load the local embedding model from {self._model_path!r}: {exc}"
                )
            ) from exc

        self._model = model
        return model

    def embed(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        """See `indexing.embedding.EmbeddingClient.embed`. Loads the model
        (once, lazily) and runs a single batched inference call across
        every text in `texts` — batching within one file's own chunks is
        intended; batching
        *across files/jobs* is a deliberately reverted design (see
        `indexing.embedding.EmbeddingClient`'s own docstring).
        """
        if not texts:
            return ()

        model = self._loaded_model()
        try:
            raw_vectors: list[Any] = list(model.embed(list(texts)))
        except Exception as exc:
            raise EmbeddingError(
                detail=f"inference failed for {len(texts)} text(s): {exc}"
            ) from exc

        if len(raw_vectors) != len(texts):
            raise EmbeddingError(
                detail=(
                    f"expected {len(texts)} vector(s) back from the embedding "
                    f"model, got {len(raw_vectors)}"
                )
            )

        vectors: list[EmbeddingVector] = []
        for raw in raw_vectors:
            values = tuple(float(component) for component in raw)
            if len(values) != EMBEDDING_DIMENSION:
                raise EmbeddingError(
                    detail=(f"expected a {EMBEDDING_DIMENSION}-dimension vector, got {len(values)}")
                )
            vectors.append(EmbeddingVector(values=values, dimension=len(values)))

        return tuple(vectors)


# Runtime confirmation (not just a docstring claim) that this class actually
# satisfies the Protocol it stands in for, mirroring
# `adapter/github/tree.py`'s own runtime check.
_: type[EmbeddingClient] = LocalONNXEmbeddingClient
