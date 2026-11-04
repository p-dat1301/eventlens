"""Local Hugging Face embedders via sentence-transformers (benchmark only).

The library is imported dynamically so runtime images and CI, which never
install the ``bench`` dependency group, can import this module. Weights must
already be in the local Hugging Face cache (``local_files_only``); the pinned
``revision`` is the snapshot commit and is recorded in every report.

Matryoshka models (Qwen3-Embedding) may be asked for a smaller trained output
size via ``dimension``; the vector is the model's own leading components,
re-normalized. Padding or truncating to fit a database column is never done.
"""

import gc
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from typing import Protocol, cast

from backend.rag.domain.search_index import EmbedderIdentity, checked_vector


class _Encoder(Protocol):
    max_seq_length: int

    def encode(  # noqa: PLR0913 - mirrors the library signature
        self,
        sentences: list[str],
        *,
        batch_size: int,
        prompt: str | None,
        normalize_embeddings: bool,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> Sequence[Sequence[float]]: ...


@dataclass(frozen=True, slots=True)
class EmbedderSpec:
    """Frozen model choice; ``native_dimension`` is the model's full output."""

    model: str
    revision: str
    native_dimension: int
    dimension: int
    pooling: str
    query_prefix: str = ""
    document_prefix: str = ""
    max_tokens: int = 1024
    batch_size: int = 16


BGE_M3 = EmbedderSpec(
    model="BAAI/bge-m3",
    revision="",
    native_dimension=1024,
    dimension=1024,
    pooling="cls",
)
QWEN3_QUERY_PREFIX = (
    "Instruct: Given a web search query, retrieve relevant passages that "
    "answer the query\nQuery:"
)
QWEN3_EMBEDDING_06B = EmbedderSpec(
    model="Qwen/Qwen3-Embedding-0.6B",
    revision="",
    native_dimension=1024,
    dimension=1024,
    pooling="last-token",
    query_prefix=QWEN3_QUERY_PREFIX,
)


def _normalize(values: Sequence[float]) -> tuple[float, ...]:
    norm = math.sqrt(sum(float(v) * float(v) for v in values))
    if norm == 0.0:
        return tuple(float(v) for v in values)
    return tuple(float(v) / norm for v in values)


@dataclass(slots=True)
class SentenceTransformerEmbedder:
    """``TextEmbedder`` over a locally cached sentence-transformers model."""

    spec: EmbedderSpec
    device: str = "cpu"
    _model: _Encoder | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Only trained Matryoshka prefixes or the full size are allowed."""
        if not self.spec.revision:
            msg = "a pinned model revision (snapshot commit) is required"
            raise ValueError(msg)
        if not 0 < self.spec.dimension <= self.spec.native_dimension:
            msg = "dimension must not exceed the model's native output"
            raise ValueError(msg)

    @property
    def identity(self) -> EmbedderIdentity:
        """Identity including revision and the effective dimension."""
        return EmbedderIdentity(
            model=self.spec.model,
            revision=self.spec.revision,
            dimension=self.spec.dimension,
            pooling=self.spec.pooling,
            query_prefix=self.spec.query_prefix,
            document_prefix=self.spec.document_prefix,
            max_tokens=self.spec.max_tokens,
        )

    def _encoder(self) -> _Encoder:
        if self._model is None:
            module = import_module("sentence_transformers")
            factory = cast("Callable[..., _Encoder]", module.SentenceTransformer)
            model = factory(
                self.spec.model,
                revision=self.spec.revision,
                device=self.device,
                local_files_only=True,
            )
            model.max_seq_length = self.spec.max_tokens
            self._model = model
        return self._model

    def _embed(
        self, texts: Sequence[str], prefix: str
    ) -> tuple[tuple[float, ...], ...]:
        raw = self._encoder().encode(
            list(texts),
            batch_size=self.spec.batch_size,
            prompt=prefix or None,
            normalize_embeddings=False,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        size = self.spec.dimension
        return tuple(
            checked_vector(_normalize([float(v) for v in row[:size]]), size)
            for row in raw
        )

    def close(self) -> None:
        """Drop the model and return accelerator memory (benchmark sequencing)."""
        self._model = None
        _ = gc.collect()
        backend = {"mps": "mps", "cuda": "cuda"}.get(self.device.split(":")[0])
        if backend is not None:
            accelerator = cast("object", getattr(import_module("torch"), backend))
            empty = cast("Callable[[], None]", getattr(accelerator, "empty_cache"))  # noqa: B009
            empty()

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Document vectors (document prefix, if the model defines one)."""
        return self._embed(texts, self.spec.document_prefix)

    def embed_queries(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Query vectors with the model's query instruction."""
        return self._embed(texts, self.spec.query_prefix)
