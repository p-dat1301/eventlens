"""Cross-encoder reranker (``BAAI/bge-reranker-v2-m3``), opt-in only.

Built by the composition root only when ``EVENTLENS_RAG_RERANK=cross-encoder``
and a pinned snapshot revision is configured. Weights load with
``local_files_only=True`` from the ``bench`` dependency group. The retrieval
use case already bounds the call (<= 40 candidates x <= 512 tokens, its own
time slice) and falls back to fused order on timeout, error or invalid
output, so this adapter can never fail a request.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from typing import Final, Protocol, cast

from backend.rag.application.ports.reranker import (
    RerankerIdentity,
    RerankOutput,
    RerankRequest,
)

BGE_RERANKER_MODEL: Final = "BAAI/bge-reranker-v2-m3"


class RerankerUnavailableError(RuntimeError):
    """Library or cached weights missing."""


class _CrossEncoder(Protocol):
    def predict(
        self,
        sentences: list[tuple[str, str]],
        *,
        batch_size: int,
        show_progress_bar: bool,
    ) -> Sequence[float]: ...


def _load(model: str, revision: str, device: str, max_length: int) -> _CrossEncoder:
    try:
        module = import_module("sentence_transformers")
    except ImportError as error:
        msg = "sentence-transformers is not installed (uv sync --group bench)"
        raise RerankerUnavailableError(msg) from error
    factory = cast("Callable[..., _CrossEncoder]", module.CrossEncoder)
    try:
        return factory(
            model,
            revision=revision,
            device=device,
            max_length=max_length,
            local_files_only=True,
        )
    except OSError as error:
        msg = "reranker weights are not in the local Hugging Face cache"
        raise RerankerUnavailableError(msg) from error


@dataclass(slots=True)
class CrossEncoderReranker:
    """``RerankerPort`` over a local cross-encoder."""

    revision: str
    model: str = BGE_RERANKER_MODEL
    device: str = "cpu"
    batch_size: int = 16
    max_length: int = 512
    loader: Callable[[str, str, str, int], _CrossEncoder] = _load
    _encoder: _CrossEncoder | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """A pinned snapshot revision is mandatory."""
        if not self.revision.strip():
            msg = "a pinned reranker snapshot revision is required"
            raise ValueError(msg)

    @property
    def identity(self) -> RerankerIdentity:
        """Model plus pinned revision."""
        return RerankerIdentity(
            reranker_id=self.model, version=f"{self.revision}:{self.max_length}"
        )

    def rerank(self, request: RerankRequest) -> RerankOutput:
        """One relevance score per candidate."""
        if self._encoder is None:
            self._encoder = self.loader(
                self.model, self.revision, self.device, self.max_length
            )
        pairs = [(request.query, candidate.text) for candidate in request.candidates]
        scores = self._encoder.predict(
            pairs, batch_size=self.batch_size, show_progress_bar=False
        )
        return RerankOutput(
            scores=tuple(
                (candidate.candidate_id, float(score))
                for candidate, score in zip(request.candidates, scores, strict=True)
            )
        )
