"""Grounded briefing and immutable article-document contracts."""

# Pydantic custom error literals are intentional contract error codes.
# ruff: noqa: EM101

from typing import Annotated, Literal, LiteralString, Self
from uuid import UUID

from pydantic import Field, HttpUrl, model_validator
from pydantic_core import PydanticCustomError

from eventlens_contracts.base import (
    CanonicalDomain,
    ContractModel,
    NonEmptyText,
    NonNegativeInt32,
    PrefixedSha256,
    UtcDatetime,
)
from eventlens_contracts.taxonomy import TopicTag

BoundedPositiveInt = Annotated[int, Field(ge=1, le=100)]


def _error(error_type: LiteralString, message: LiteralString) -> PydanticCustomError:
    return PydanticCustomError(error_type, message)


class EvidenceSpan(ContractModel):
    """Immutable Unicode code-point range in one document content revision."""

    span_id: UUID
    article_id: UUID
    content_hash: PrefixedSha256
    start_offset: NonNegativeInt32
    end_offset: Annotated[int, Field(ge=1, le=2_147_483_647)]
    excerpt: NonEmptyText
    offset_basis: Literal["unicode_codepoint"]

    @model_validator(mode="after")
    def enforce_offset_order(self) -> Self:
        """Reject empty or reversed source ranges."""
        if self.end_offset <= self.start_offset:
            raise _error(
                "invalid_evidence_offsets", "end_offset must exceed start_offset"
            )
        return self


class ArticleDocument(ContractModel):
    """Immutable searchable document separate from canonical Article v1."""

    schema_version: Literal["article_document.v1"]
    article_id: UUID
    source_id: UUID
    title: NonEmptyText
    normalized_content: NonEmptyText
    canonical_url: HttpUrl
    source_name: NonEmptyText
    source_domain: CanonicalDomain
    source_published_at: UtcDatetime | None
    ingested_at: UtcDatetime
    processed_at: UtcDatetime
    content_hash: PrefixedSha256
    visibility: Literal["internal"]
    evidence_spans: tuple[EvidenceSpan, ...]

    @model_validator(mode="after")
    def enforce_evidence(self) -> Self:
        """Bind immutable spans to this exact normalized-content revision."""
        span_ids = tuple(span.span_id for span in self.evidence_spans)
        if len(set(span_ids)) != len(span_ids):
            raise _error("duplicate_span_id", "span_id values must be unique")
        for span in self.evidence_spans:
            if span.article_id != self.article_id:
                raise _error(
                    "span_article_id_mismatch",
                    "span article_id must match document article_id",
                )
            if span.content_hash != self.content_hash:
                raise _error(
                    "span_content_hash_mismatch",
                    "span content_hash must match document content_hash",
                )
            if span.end_offset > len(self.normalized_content):
                raise _error(
                    "evidence_offset_out_of_range",
                    "span offsets must fit normalized_content",
                )
            if (
                self.normalized_content[span.start_offset : span.end_offset]
                != span.excerpt
            ):
                raise _error(
                    "evidence_excerpt_mismatch",
                    "span excerpt must match normalized_content offsets",
                )
        if self.processed_at < self.ingested_at:
            raise _error(
                "invalid_processing_time", "processed_at must not precede ingested_at"
            )
        return self


class BriefingQuery(ContractModel):
    """Bounded analyst request over source-publication time."""

    schema_version: Literal["briefing_query.v1"]
    query: NonEmptyText
    source_domains: tuple[CanonicalDomain, ...] = ()
    topic_filters: tuple[TopicTag, ...] = ()
    entity_ids: tuple[UUID, ...] = ()
    published_after: UtcDatetime | None = None
    published_before: UtcDatetime | None = None
    publication_time_basis: Literal["source_published_at"]
    result_limit: BoundedPositiveInt
    context_budget: Annotated[int, Field(ge=1, le=12_000)]

    @model_validator(mode="after")
    def enforce_query_filters(self) -> Self:
        """Reject ambiguous filter sets and invalid publication ranges."""
        if (
            self.published_after is not None
            and self.published_before is not None
            and self.published_after > self.published_before
        ):
            raise _error(
                "invalid_publication_range",
                "published_after must not exceed published_before",
            )
        if len(set(self.source_domains)) != len(self.source_domains):
            raise _error(
                "duplicate_source_domain", "query source domains must be unique"
            )
        if len(set(self.topic_filters)) != len(self.topic_filters):
            raise _error("duplicate_topic_filter", "query topic filters must be unique")
        if len(set(self.entity_ids)) != len(self.entity_ids):
            raise _error("duplicate_entity_id", "query entity ids must be unique")
        return self


class Claim(ContractModel):
    """One material factual proposition in a briefing."""

    claim_id: UUID
    text: NonEmptyText


class Citation(ContractModel):
    """Claim-bound pointer to immutable source evidence."""

    citation_id: UUID
    claim_id: UUID
    article_id: UUID
    evidence_span_id: UUID
    source_title: NonEmptyText
    source_reference: HttpUrl


class Uncertainty(ContractModel):
    """Explicit evidence limitation or abstention reason."""

    uncertainty_id: UUID
    category: Literal[
        "insufficient_evidence",
        "ambiguity",
        "source_conflict",
        "temporal_uncertainty",
    ]
    explanation: NonEmptyText
    claim_id: UUID | None = None


class BriefingAnswer(ContractModel):
    """Grounded answer with deterministic claim-to-evidence validation."""

    schema_version: Literal["briefing_answer.v1"]
    answer: NonEmptyText
    claims: tuple[Claim, ...]
    citations: tuple[Citation, ...]
    uncertainties: tuple[Uncertainty, ...]
    source_documents: tuple[ArticleDocument, ...]
    retrieved_at: UtcDatetime
    model_version: NonEmptyText
    index_version: NonEmptyText

    @model_validator(mode="after")
    def enforce_grounding(self) -> Self:  # noqa: C901, PLR0912
        """Require every material claim to bind valid immutable evidence."""
        claim_ids = tuple(claim.claim_id for claim in self.claims)
        citation_ids = tuple(citation.citation_id for citation in self.citations)
        uncertainty_ids = tuple(item.uncertainty_id for item in self.uncertainties)
        if len(set(claim_ids)) != len(claim_ids):
            raise _error("duplicate_claim_id", "claim_id values must be unique")
        if len(set(citation_ids)) != len(citation_ids):
            raise _error("duplicate_citation_id", "citation_id values must be unique")
        if len(set(uncertainty_ids)) != len(uncertainty_ids):
            raise _error(
                "duplicate_uncertainty_id", "uncertainty_id values must be unique"
            )
        documents = {
            document.article_id: document for document in self.source_documents
        }
        if len(documents) != len(self.source_documents):
            raise _error(
                "duplicate_source_document",
                "source document article_id values must be unique",
            )
        cited_claim_ids: set[UUID] = set()
        for citation in self.citations:
            if citation.claim_id not in claim_ids:
                raise _error(
                    "unknown_citation_claim", "citation claim_id must reference a claim"
                )
            document = documents.get(citation.article_id)
            if document is None:
                raise _error(
                    "unknown_citation_article",
                    "citation article_id must reference a document",
                )
            matching_spans = tuple(
                span
                for span in document.evidence_spans
                if span.span_id == citation.evidence_span_id
            )
            if not matching_spans:
                raise _error(
                    "unknown_evidence_span",
                    "citation must reference an article evidence span",
                )
            if citation.source_title != document.title:
                raise _error(
                    "citation_title_mismatch",
                    "citation source_title must match document title",
                )
            if citation.source_reference != document.canonical_url:
                raise _error(
                    "citation_reference_mismatch",
                    "citation source_reference must match document canonical_url",
                )
            cited_claim_ids.add(citation.claim_id)
        if any(claim_id not in cited_claim_ids for claim_id in claim_ids):
            raise _error(
                "claim_missing_citation", "every claim must have at least one citation"
            )
        for uncertainty in self.uncertainties:
            if (
                uncertainty.claim_id is not None
                and uncertainty.claim_id not in claim_ids
            ):
                raise _error(
                    "unknown_uncertainty_claim",
                    "uncertainty claim_id must reference a claim",
                )
        if not self.claims and not any(
            item.category == "insufficient_evidence" for item in self.uncertainties
        ):
            raise _error(
                "abstention_missing_uncertainty",
                "answers without claims require insufficient_evidence uncertainty",
            )
        return self
