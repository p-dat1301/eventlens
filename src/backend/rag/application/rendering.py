"""Deterministic server rendering of validated answers and abstentions.

The final ``answer`` text is only ever ``BriefingAnswer.rendered_answer()``
over validated display blocks: claims in draft order, then model
uncertainties, then server uncertainties. Uncertainty prose comes from the
versioned policy templates; nothing is appended after validation.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError

from backend.rag.domain.answer import (
    BriefingAnswer,
    ClaimDisplayBlock,
    DisplayBlock,
    Uncertainty,
    UncertaintyDisplayBlock,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.synthesis import GroundedDraft, TrustedSourceRegistry
from backend.rag.domain.synthesis_errors import (
    GroundingFailureCode,
    SynthesisGroundingError,
)
from backend.rag.domain.synthesis_policy import (
    ModelReasonCode,
    ServerReasonCode,
    SynthesisPolicy,
)


def template_uncertainty(
    reason_code: ServerReasonCode | ModelReasonCode,
    policy: SynthesisPolicy,
    ids: Callable[[], UUID],
) -> Uncertainty:
    """Server-owned uncertainty from the versioned template (no claim link)."""
    template = policy.template(reason_code)
    return Uncertainty(
        uncertainty_id=ids(),
        category=template.category,
        explanation=template.text,
        claim_id=None,
    )


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value


def _answer(  # noqa: PLR0913, PLR0917
    grounded: GroundedDraft,
    uncertainties: tuple[Uncertainty, ...],
    documents: tuple[ArticleDocument, ...],
    retrieved_at: datetime,
    model_version: str,
    index_version: str,
) -> BriefingAnswer:
    blocks: list[DisplayBlock] = [
        ClaimDisplayBlock(kind="claim", claim_id=claim.claim_id)
        for claim in grounded.claims
    ]
    blocks.extend(
        UncertaintyDisplayBlock(kind="uncertainty", uncertainty_id=item.uncertainty_id)
        for item in uncertainties
    )
    text = "\n\n".join(
        [claim.text for claim in grounded.claims]
        + [item.explanation for item in uncertainties]
    )
    try:
        return BriefingAnswer(
            schema_version="briefing_answer.v1",
            claims=grounded.claims,
            citations=grounded.citations,
            uncertainties=uncertainties,
            display_blocks=tuple(blocks),
            answer=text,
            source_documents=documents,
            retrieved_at=_utc(retrieved_at),
            model_version=model_version,
            index_version=index_version,
        )
    except ValidationError:
        raise SynthesisGroundingError(
            GroundingFailureCode.ANSWER_CONTRACT_INVALID
        ) from None


def render_answer(  # noqa: PLR0913
    grounded: GroundedDraft,
    registry: TrustedSourceRegistry,
    *,
    server_uncertainties: tuple[Uncertainty, ...],
    retrieved_at: datetime,
    model_version: str,
    index_version: str,
) -> BriefingAnswer:
    """Validated answer citing only trusted, current source documents."""
    cited: list[UUID] = []
    for citation in grounded.citations:
        if citation.article_id not in cited:
            cited.append(citation.article_id)
    sources = registry.sources
    if any(article_id not in sources for article_id in cited):
        raise SynthesisGroundingError(GroundingFailureCode.ANSWER_CONTRACT_INVALID)
    return _answer(
        grounded,
        grounded.uncertainties + server_uncertainties,
        tuple(sources[article_id].document for article_id in cited),
        retrieved_at,
        model_version,
        index_version,
    )


def abstention_answer(  # noqa: PLR0913
    reason_code: ServerReasonCode | ModelReasonCode,
    policy: SynthesisPolicy,
    *,
    ids: Callable[[], UUID],
    retrieved_at: datetime,
    model_version: str,
    index_version: str,
) -> BriefingAnswer:
    """No-claims answer whose only text is the reason's template."""
    empty = GroundedDraft(
        claims=(), citations=(), uncertainties=(), evidence_status="insufficient"
    )
    return _answer(
        empty,
        (template_uncertainty(reason_code, policy, ids),),
        (),
        retrieved_at,
        model_version,
        index_version,
    )
