"""Deterministic scripted reranker fake for tests and the smoke runner."""

from collections.abc import Callable
from dataclasses import dataclass, field

from eventlens_rag.ports.reranker import RerankerIdentity, RerankOutput, RerankRequest

type RerankScript = Callable[[RerankRequest], RerankOutput]


class ScriptedRerankerError(RuntimeError):
    """Injected reranker failure."""

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("scripted reranker failure")


@dataclass(slots=True)
class ScriptedReranker:
    """Run a caller-provided script; records every request it receives."""

    script: RerankScript
    requests: list[RerankRequest] = field(default_factory=list[RerankRequest])
    reranker_id: str = "scripted-reranker"
    version: str = "v1"

    @property
    def identity(self) -> RerankerIdentity:
        """Identity of the fake."""
        return RerankerIdentity(self.reranker_id, self.version)

    def rerank(self, request: RerankRequest) -> RerankOutput:
        """Record and delegate."""
        self.requests.append(request)
        return self.script(request)


def reverse_script(request: RerankRequest) -> RerankOutput:
    """Valid output that reverses the input order."""
    return RerankOutput(
        scores=tuple(
            (candidate.candidate_id, float(index))
            for index, candidate in enumerate(request.candidates)
        )
    )


def failing_script(request: RerankRequest) -> RerankOutput:
    """Always fails (models a reranker error)."""
    del request
    raise ScriptedRerankerError
