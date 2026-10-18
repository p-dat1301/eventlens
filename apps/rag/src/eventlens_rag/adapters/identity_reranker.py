"""Default reranker: keeps fused order exactly, including ties."""

from eventlens_rag.ports.reranker import RerankerIdentity, RerankOutput, RerankRequest

IDENTITY_RERANKER = RerankerIdentity(reranker_id="identity-reranker", version="v1")


class IdentityReranker:
    """Score candidates by descending input position (no model, no network)."""

    @property
    def identity(self) -> RerankerIdentity:
        """Stable identity recorded in results."""
        return IDENTITY_RERANKER

    def rerank(self, request: RerankRequest) -> RerankOutput:
        """Strictly decreasing scores, so the fused order (and ties) survive."""
        total = len(request.candidates)
        return RerankOutput(
            scores=tuple(
                (candidate.candidate_id, float(total - index))
                for index, candidate in enumerate(request.candidates)
            )
        )
