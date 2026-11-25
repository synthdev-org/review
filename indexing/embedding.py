"""indexing.embedding — the ``EmbeddingClient`` Protocol and the models
around it (US4), per the indexing delivery plan's Phase 4 section (Unit D).

Defines the domain-side contract for turning a file's own chunk texts into
fixed-dimension vectors, per ``rag-techniques.md``'s "real embeddings, not a
bare diff" direction and ADR-0007 ("indexing stays LLM-free" — this is a
plain sentence-embedding model, never a generative LLM call). No infra
import anywhere in this module (no ``fastembed``/ONNX runtime) — per
``docs/rules/architecture.md``'s zero-infra-imports domain-package rule;
``adapter/embedding/local_onnx.py`` is the one concrete implementation,
wired in only at the composition root (``apps/worker/main.py``, Phase 7).

Justified as a domain-defined Protocol regardless of implementation count,
per the plan's Assumption 5 — the same reasoning ``indexing/tree.py``'s
``RepoTreeFetcher`` docstring already states for its own Protocol:
``indexing/pipeline.py`` (Phase 7, this Protocol's one consumer) is a
domain package and may not import an adapter module directly, independent
of whether a second implementation of this Protocol ever ships.

``EmbeddingClient.embed`` is a **plain, synchronous** method, not
``async def`` — unlike ``RepoTreeFetcher``'s methods, which perform real
network I/O. Per the plan's Phase 7 section ("CPU-bound work runs off the
event loop"), local ONNX inference is CPU-bound, not I/O-bound, and Phase
7's pipeline runs it via ``asyncio.to_thread`` — the same mechanism already
used elsewhere in this codebase for synchronous, blocking work
(``adapter/github/auth.py``'s RS256 signing,
``adapter/analyzers/subprocess_runner.py``'s analyzer sub-steps). An
``async def`` signature here would falsely advertise real async I/O this
method never performs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator


class EmbeddingVector(BaseModel):
    """One fixed-dimension embedding vector, per the plan's Phase 4
    section. ``dimension`` is carried alongside ``values`` (rather than
    left implicit as ``len(values)``) so a consumer can validate shape
    without recomputing it, and so a stored/round-tripped vector's
    dimension is self-describing — enforced at construction, so the two
    can never silently disagree.
    """

    model_config = ConfigDict(frozen=True)

    values: tuple[float, ...]
    dimension: int

    @model_validator(mode="after")
    def _dimension_matches_values(self) -> EmbeddingVector:
        if self.dimension != len(self.values):
            raise ValueError(
                f"dimension={self.dimension} does not match len(values)={len(self.values)}"
            )
        return self


class EmbeddingError(Exception):
    """Raised by ``EmbeddingClient.embed`` on any failure — model-load
    failure, inference failure, or a malformed/empty result — never a raw
    provider/runtime exception, per architecture.md's exception-type rule
    and the plan's Assumption 12.

    Never caught inside an ``EmbeddingClient`` implementation's own
    module beyond the point of translation; Phase 7's pipeline is the one
    place US10's per-file isolation catches it (per-file, not job-wide —
    see that phase's own docstring for why an earlier job-wide batched
    design was reverted).
    """

    def __init__(self, *, detail: str) -> None:
        super().__init__(f"embedding failed: {detail}")
        self.detail = detail


@runtime_checkable
class EmbeddingClient(Protocol):
    """Turns a sequence of chunk texts into one fixed-dimension vector per
    text, per US4. Implemented by ``adapter/embedding/local_onnx.py``'s
    ``LocalONNXEmbeddingClient``.

    The signature accepts a ``Sequence[str]`` (a single file's own chunk
    texts can number more than one) but Phase 7 calls it once **per file**,
    not once per job — an earlier job-wide batched design was tried and
    reverted after review found it widened a single bad chunk's blast
    radius to an entire run (see the plan's Phase 7 section).
    """

    def embed(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        """Embed every text in ``texts``, in order — the returned tuple's
        length and ordering match ``texts`` exactly. Raises
        ``EmbeddingError`` on any failure; never returns a partial result.
        """
        ...
