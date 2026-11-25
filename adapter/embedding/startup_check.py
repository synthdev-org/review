"""adapter.embedding.startup_check — startup check that the bundled local
embedding model loads and runs inference successfully.

Per ``spec-indexing.md``'s Non-Functional Requirements ("the Worker
verifies the bundled local embedding model loads successfully at startup,
failing fast with a clear error rather than the first ``INDEX`` job
silently hanging or failing deep inside US4") and the plan's Phase 4
section: **mirrors ``adapter/storage/pgvector_check.py``'s fail-fast
pattern exactly** — attempt a real load/inference call once, here, and let
a typed exception (never a generic one) propagate to the composition root
(``apps/worker/main.py``), which decides what "fail fast" means for the
process (log + ``SystemExit(1)``), the same split ``pgvector_check.py``
and ``llmgateway/startup_check.py`` both already use.

Lives in ``adapter/embedding``, not the ``indexing`` domain package —
unlike ``llmgateway/startup_check.py`` (generic over any ``LLMClient``
Protocol implementation), this check is scoped to "does *the concrete,
locally-bundled* model load and infer," the same adapter-scoped shape
``adapter/storage/pgvector_check.py`` already has for its own
extension-presence check. **Scoped to "does it load and infer" only** —
the model artifact's checksum is already verified at Docker build time
(the Dockerfile's ``embedding-model`` stage, per Assumption 4); this check
does not re-verify it at runtime.
"""

from __future__ import annotations

from adapter.embedding.local_onnx import LocalONNXEmbeddingClient
from indexing.embedding import EmbeddingError

# A minimal, low-cost probe text — this call exists purely to prove the
# bundled model loads and produces a well-formed vector, not to embed
# anything meaningful. Mirrors `llmgateway/startup_check.py`'s own
# `_PROBE_PROMPT` shape.
_PROBE_TEXT = "ping"


def check_embedding_model_loaded(client: LocalONNXEmbeddingClient) -> None:
    """Attempt one real load/inference call through `client`. Raises
    `indexing.embedding.EmbeddingError` (never a raw `fastembed`/ONNX
    exception, since `LocalONNXEmbeddingClient.embed` already translates
    every failure per architecture.md's exception-type rule) if the model
    fails to load or produce a well-formed vector.
    """
    vectors = client.embed((_PROBE_TEXT,))
    if len(vectors) != 1:
        raise EmbeddingError(detail=f"expected exactly one probe vector back, got {len(vectors)}")
