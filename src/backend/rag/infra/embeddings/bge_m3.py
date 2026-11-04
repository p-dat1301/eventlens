"""BGE-M3 dense embeddings via a locally cached sentence-transformers model.

Activation is explicit: ``BgeM3Embeddings`` is only built by the composition
root when ``EVENTLENS_RAG_SEARCH_EMBEDDINGS=bge-m3`` *and* a pinned snapshot
revision is configured. Weights are loaded with ``local_files_only=True`` (no
download, ever); the optional ``bench`` dependency group provides the
library, which CI and runtime images do not install. The library is imported
lazily so this module imports without it.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from typing import Protocol, cast

from backend.rag.domain.search_embedding import (
    BGE_M3,
    SearchEmbeddingIdentity,
    checked_vector,
    l2_normalize,
)


class ModelUnavailableError(RuntimeError):
    """The model library or the cached weights are missing."""

    code: str = "embedding_model_unavailable"


class _Encoder(Protocol):
    max_seq_length: int

    def encode(
        self,
        sentences: list[str],
        *,
        batch_size: int,
        normalize_embeddings: bool,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> Sequence[Sequence[float]]: ...


def _load(model: str, revision: str, device: str) -> _Encoder:
    try:
        module = import_module("sentence_transformers")
    except ImportError as error:
        msg = "sentence-transformers is not installed (uv sync --group bench)"
        raise ModelUnavailableError(msg) from error
    factory = cast("Callable[..., _Encoder]", module.SentenceTransformer)
    try:
        return factory(model, revision=revision, device=device, local_files_only=True)
    except OSError as error:
        msg = "model weights are not in the local Hugging Face cache"
        raise ModelUnavailableError(msg) from error


@dataclass(slots=True)
class BgeM3Embeddings:
    """``BatchEmbeddingPort`` for ``BAAI/bge-m3`` (1024 dims, CLS, normalized)."""

    revision: str
    device: str = "cpu"
    batch_size: int = 16
    max_tokens: int = 1024
    loader: Callable[[str, str, str], _Encoder] = _load
    _model: _Encoder | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """A pinned snapshot revision is mandatory (reproducible identity)."""
        if not self.revision.strip():
            msg = "a pinned BGE-M3 snapshot revision is required"
            raise ValueError(msg)

    @property
    def identity(self) -> SearchEmbeddingIdentity:
        """``bge-m3-1024.v1`` plus the pinned revision."""
        return SearchEmbeddingIdentity(
            identity=BGE_M3.identity,
            model=BGE_M3.model,
            dimensions=BGE_M3.dimensions,
            revision=self.revision,
            max_tokens=self.max_tokens,
        )

    def _encoder(self) -> _Encoder:
        if self._model is None:
            model = self.loader(BGE_M3.model, self.revision, self.device)
            model.max_seq_length = self.max_tokens
            self._model = model
        return self._model

    def _embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        rows = self._encoder().encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=False,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return tuple(
            checked_vector(
                l2_normalize(tuple(float(v) for v in row)), BGE_M3.dimensions
            )
            for row in rows
        )

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Document vectors (BGE-M3 dense uses no instruction prefix)."""
        return self._embed(texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        """Query vector."""
        return self._embed([text])[0]

    def close(self) -> None:
        """Drop the model (benchmarks load one model at a time)."""
        self._model = None
