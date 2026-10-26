"""Evidence selection, trusted source registry and mechanical grounding.

The registry is built from the *current* ``ArticleDocument`` read in one batch,
never from model output. Only spans actually sent in the provider payload are
citable; every other span the server knows (retrieved but trimmed for budget)
is recognised as known-but-not-sent. Grounding checks IDs, ownership, content
hash, code-point offsets/excerpt and fingerprint; it proves mechanical
linkage only, not that a claim is semantically supported (T9 evaluation).
"""

from collections.abc import Callable, Mapping
from hashlib import sha256
from uuid import UUID

from pydantic import ValidationError

from backend.rag.application.ports.retrieval import CurrentArticleState
from backend.rag.domain.answer import Citation, Claim, Uncertainty
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import (
    Branch,
    RetrievalResult,
    RetrievedArticle,
    RetrievedSpan,
)
from backend.rag.domain.synthesis import (
    GroundedDraft,
    SelectedArticle,
    TrustedSource,
    TrustedSourceRegistry,
)
from backend.rag.domain.synthesis_draft import SynthesisDraft
from backend.rag.domain.synthesis_errors import (
    GroundingFailureCode,
    SynthesisConfigurationError,
    SynthesisEvidenceStaleError,
    SynthesisGroundingError,
)
from backend.rag.domain.synthesis_policy import SynthesisPolicy
from backend.shared.contracts.evidence import EvidenceSpan


def _fingerprint(text: str) -> str:
    return f"sha256:{sha256(text.encode()).hexdigest()}"


def _consistent(article: RetrievedArticle) -> bool:
    return bool(article.spans) and all(
        item.span.article_id == article.article_id
        and item.span.content_hash == article.content_hash
        for item in article.spans
    )


def select_evidence(
    retrieval: RetrievalResult, max_articles: int
) -> tuple[SelectedArticle, ...]:
    """Keep T5 order, at most ``max_articles``; reject inconsistent input."""
    if max_articles < 1:
        message = "max_articles must be positive"
        raise SynthesisConfigurationError(message)
    articles = retrieval.articles[:max_articles]
    span_ids = [item.span.span_id for article in articles for item in article.spans]
    article_ids = [article.article_id for article in articles]
    if (
        len(set(span_ids)) != len(span_ids)
        or len(set(article_ids)) != len(article_ids)
        or not all(_consistent(article) for article in articles)
    ):
        message = "retrieval result evidence is inconsistent"
        raise SynthesisConfigurationError(message)
    return tuple(
        SelectedArticle(article=article, spans=article.spans) for article in articles
    )


def _in_scope(document: ArticleDocument, query: BriefingQuery) -> bool:
    if query.source_domains and document.source_domain not in query.source_domains:
        return False
    published = document.source_published_at
    if query.published_after is not None and (
        published is None or published < query.published_after
    ):
        return False
    return query.published_before is None or (
        published is not None and published < query.published_before
    )


def _metadata_eligible(state: CurrentArticleState, query: BriefingQuery) -> bool:
    if not (query.topic_filters or query.entity_ids):
        return True
    metadata = state.metadata
    if metadata is None:
        return False
    if query.topic_filters and not set(query.topic_filters) & set(metadata.topics):
        return False
    wanted = set(query.entity_ids)
    return not wanted or any(entity.entity_id in wanted for entity in metadata.entities)


def _span_current(span: EvidenceSpan, document: ArticleDocument) -> bool:
    return (
        span.article_id == document.article_id
        and span.content_hash == document.content_hash
        and span.end_offset <= len(document.normalized_content)
        and document.normalized_content[span.start_offset : span.end_offset]
        == span.excerpt
        and span.span_fingerprint == _fingerprint(span.excerpt)
    )


def _vector_current(
    article: RetrievedArticle,
    spans: tuple[RetrievedSpan, ...],
    state: CurrentArticleState,
) -> bool:
    """A vector-only span stays eligible only on the same active generation."""
    vector_only = any(item.branches == (Branch.VECTOR,) for item in spans)
    return not vector_only or (
        article.vector_generation is not None
        and article.vector_generation == state.active_index_generation
    )


def _trusted_document(
    document: ArticleDocument, spans: tuple[EvidenceSpan, ...]
) -> ArticleDocument:
    """Validated construction of the current document with the sent spans."""
    try:
        return ArticleDocument.model_validate(
            {**dict(document), "evidence_spans": spans}
        )
    except ValidationError:
        raise SynthesisEvidenceStaleError from None


def build_registry(
    selected: tuple[SelectedArticle, ...],
    sent_span_ids: frozenset[UUID],
    current: tuple[CurrentArticleState, ...],
    query: BriefingQuery,
) -> TrustedSourceRegistry:
    """Bind sent spans to current, eligible revisions or raise stale.

    Raises ``SynthesisEvidenceStaleError`` when any article with a sent span
    is missing (tombstoned/corrected away), changed hash, left the query scope,
    lost the metadata eligibility used for the query, or no longer contains
    the exact sent span. The whole answer fails; nothing is re-selected.
    """
    states: Mapping[UUID, CurrentArticleState] = {
        state.document.article_id: state for state in current
    }
    known = {item.span.span_id for entry in selected for item in entry.spans}
    if not sent_span_ids <= known:
        message = "sent spans must come from the selected evidence"
        raise SynthesisConfigurationError(message)
    sources: dict[UUID, TrustedSource] = {}
    owner: dict[UUID, UUID] = {}
    for entry in selected:
        article = entry.article
        for item in entry.spans:
            owner[item.span.span_id] = article.article_id
        sent = tuple(item for item in entry.spans if item.span.span_id in sent_span_ids)
        if not sent:
            continue
        state = states.get(article.article_id)
        if state is None:
            raise SynthesisEvidenceStaleError
        document = state.document
        if (
            document.content_hash != article.content_hash
            or not _in_scope(document, query)
            or not _metadata_eligible(state, query)
            or not _vector_current(article, sent, state)
            or not all(_span_current(item.span, document) for item in sent)
        ):
            raise SynthesisEvidenceStaleError
        spans = tuple(item.span for item in sent)
        sources[article.article_id] = TrustedSource(
            document=_trusted_document(document, spans), sent_spans=spans
        )
    # ``span_owner`` also lists known-but-unsent spans so grounding can tell a
    # valid span that was never sent (``span_not_sent``) from an unknown ID.
    return TrustedSourceRegistry(sources=sources, span_owner=owner)


def _check_shape(draft: SynthesisDraft) -> None:
    refs = [claim.claim_ref for claim in draft.claims]
    if len(set(refs)) != len(refs):
        raise SynthesisGroundingError(GroundingFailureCode.DUPLICATE_CLAIM_REF)
    if any(
        item.claim_ref is not None and item.claim_ref not in refs
        for item in draft.uncertainties
    ):
        raise SynthesisGroundingError(GroundingFailureCode.DANGLING_CLAIM_REF)
    insufficient = draft.evidence_status == "insufficient"
    if insufficient == bool(draft.claims):
        raise SynthesisGroundingError(GroundingFailureCode.STATUS_MISMATCH)
    for claim in draft.claims:
        ids = claim.evidence_span_ids
        if not ids:
            raise SynthesisGroundingError(GroundingFailureCode.CLAIM_WITHOUT_CITATION)
        if len(set(ids)) != len(ids):
            raise SynthesisGroundingError(GroundingFailureCode.DUPLICATE_SPAN_REF)
        temporal = claim.temporal_claim
        if temporal is not None and not set(temporal.evidence_span_ids) <= set(ids):
            raise SynthesisGroundingError(
                GroundingFailureCode.TEMPORAL_EVIDENCE_NOT_CITED
            )


def _resolve(
    span_id: UUID, registry: TrustedSourceRegistry
) -> tuple[TrustedSource, EvidenceSpan]:
    owner = registry.span_owner.get(span_id)
    if owner is None:
        raise SynthesisGroundingError(GroundingFailureCode.UNKNOWN_SPAN)
    found = registry.lookup(span_id) if owner in registry.sources else None
    if found is None:
        raise SynthesisGroundingError(GroundingFailureCode.SPAN_NOT_SENT)
    source, span = found
    document = source.document
    if span.article_id != document.article_id:
        raise SynthesisGroundingError(GroundingFailureCode.WRONG_ARTICLE)
    if span.content_hash != document.content_hash:
        raise SynthesisGroundingError(GroundingFailureCode.CONTENT_HASH_MISMATCH)
    if not 0 <= span.start_offset < span.end_offset <= len(document.normalized_content):
        raise SynthesisGroundingError(GroundingFailureCode.OFFSET_MISMATCH)
    if document.normalized_content[span.start_offset : span.end_offset] != (
        span.excerpt
    ):
        raise SynthesisGroundingError(GroundingFailureCode.EXCERPT_MISMATCH)
    if span.span_fingerprint != _fingerprint(span.excerpt):
        raise SynthesisGroundingError(GroundingFailureCode.FINGERPRINT_MISMATCH)
    if not any(item.span_id == span.span_id for item in document.evidence_spans):
        raise SynthesisGroundingError(GroundingFailureCode.SPAN_NOT_SENT)
    return source, span


def validate_draft(
    draft: SynthesisDraft,
    registry: TrustedSourceRegistry,
    policy: SynthesisPolicy,
    ids: Callable[[], UUID],
) -> GroundedDraft:
    """Mechanically ground a strict draft; server assigns every ID and URL."""
    _check_shape(draft)
    claims: list[Claim] = []
    citations: list[Citation] = []
    claim_ids: dict[str, UUID] = {}
    try:
        for draft_claim in draft.claims:
            resolved = [
                _resolve(span_id, registry) for span_id in draft_claim.evidence_span_ids
            ]
            claim = Claim(
                claim_id=ids(),
                text=draft_claim.text,
                temporal_claim=draft_claim.temporal_claim,
            )
            claim_ids[draft_claim.claim_ref] = claim.claim_id
            claims.append(claim)
            citations.extend(
                Citation(
                    citation_id=ids(),
                    claim_id=claim.claim_id,
                    article_id=source.document.article_id,
                    evidence_span_id=span.span_id,
                    source_title=source.document.title,
                    source_reference=source.document.canonical_url,
                )
                for source, span in resolved
            )
        uncertainties = tuple(
            Uncertainty(
                uncertainty_id=ids(),
                category=policy.template(item.reason_code).category,
                explanation=policy.template(item.reason_code).text,
                claim_id=None if item.claim_ref is None else claim_ids[item.claim_ref],
            )
            for item in draft.uncertainties
        )
    except ValidationError:
        raise SynthesisGroundingError(
            GroundingFailureCode.ANSWER_CONTRACT_INVALID
        ) from None
    return GroundedDraft(
        claims=tuple(claims),
        citations=tuple(citations),
        uncertainties=uncertainties,
        evidence_status=draft.evidence_status,
    )
