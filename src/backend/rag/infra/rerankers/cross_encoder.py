"""Local cross-encoder reranker (bge-reranker-v2-m3) behind ``RerankerPort``.

Benchmark only: the library is imported dynamically, weights come from the
local cache at a pinned revision, and nothing selects this adapter in the API
or worker composition. Inputs longer than ``max_tokens`` are truncated by the
model tokenizer; evidence text and offsets are never changed.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from typing import Protocol, cast

from backend.rag.application.ports.reranker import (
    RerankerIdentity,
    RerankOutput,
    RerankRequest,
)

BGE_RERANKER_V2_M3 = "BAAI/bge-reranker-v2-m3"


class _CrossEncoder(Protocol):
    def predict(
        self,
        sentences: list[tuple[str, str]],
        *,
        batch_size: int,
        show_progress_bar: bool,
    ) -> Sequence[float]: ...


@dataclass(slots=True)
class CrossEncoderReranker:
    """Scores (query, candidate) pairs; returns one score per candidate ID."""

    revision: str
    model: str = BGE_RERANKER_V2_M3
    device: str = "cpu"
    max_tokens: int = 512
    batch_size: int = 8
    _encoder: _CrossEncoder | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """A pinned snapshot revision is mandatory."""
        if not self.revision:
            msg = "a pinned model revision (snapshot commit) is required"
            raise ValueError(msg)

    @property
    def identity(self) -> RerankerIdentity:
        """Model and pinned revision."""
        return RerankerIdentity(reranker_id=self.model, version=self.revision)

    def _model(self) -> _CrossEncoder:
        if self._encoder is None:
            module = import_module("sentence_transformers")
            factory = cast("Callable[..., _CrossEncoder]", module.CrossEncoder)
            self._encoder = factory(
                self.model,
                revision=self.revision,
                device=self.device,
                max_length=self.max_tokens,
                local_files_only=True,
            )
        return self._encoder

    def rerank(self, request: RerankRequest) -> RerankOutput:
        """Score every candidate exactly once."""
        if not request.candidates:
            return RerankOutput(scores=())
        scores = self._model().predict(
            [(request.query, item.text) for item in request.candidates],
            batch_size=self.batch_size,
            show_progress_bar=False,
        )
        return RerankOutput(
            scores=tuple(
                (item.candidate_id, float(score))
                for item, score in zip(request.candidates, scores, strict=True)
            )
        )
