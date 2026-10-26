"""Validated claim blocks and deterministic briefing display."""

# ruff: noqa: EM101

from typing import Annotated, Literal, LiteralString, Self
from uuid import UUID

from pydantic import Field, HttpUrl, model_validator
from pydantic_core import PydanticCustomError

from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.temporal import TemporalClaim
from backend.shared.contracts.base import ContractModel, NonEmptyText, UtcDatetime


def _error(error_type: LiteralString, message: LiteralString) -> PydanticCustomError:
    return PydanticCustomError(error_type, message)


class Claim(ContractModel):
    """One material factual proposition with optional occurrence-time claim."""

    claim_id: UUID
    text: NonEmptyText
    temporal_claim: TemporalClaim | None


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


class ClaimDisplayBlock(ContractModel):
    """Display one validated claim exactly once."""

    kind: Literal["claim"]
    claim_id: UUID


class UncertaintyDisplayBlock(ContractModel):
    """Display one validated uncertainty exactly once."""

    kind: Literal["uncertainty"]
    uncertainty_id: UUID


DisplayBlock = Annotated[
    ClaimDisplayBlock | UncertaintyDisplayBlock,
    Field(discriminator="kind"),
]


class BriefingAnswer(ContractModel):
    """Grounded answer rendered only from validated claim and uncertainty blocks."""

    schema_version: Literal["briefing_answer.v1"]
    claims: tuple[Claim, ...]
    citations: tuple[Citation, ...]
    uncertainties: tuple[Uncertainty, ...]
    display_blocks: tuple[DisplayBlock, ...]
    answer: NonEmptyText
    source_documents: tuple[ArticleDocument, ...]
    retrieved_at: UtcDatetime
    model_version: NonEmptyText
    index_version: NonEmptyText

    def rendered_answer(self) -> str:
        """Render display blocks in input order after model validation."""
        claim_text = {claim.claim_id: claim.text for claim in self.claims}
        uncertainty_text = {
            uncertainty.uncertainty_id: uncertainty.explanation
            for uncertainty in self.uncertainties
        }
        return "\n\n".join(
            claim_text[block.claim_id]
            if isinstance(block, ClaimDisplayBlock)
            else uncertainty_text[block.uncertainty_id]
            for block in self.display_blocks
        )

    @model_validator(mode="after")
    def enforce_grounding(self) -> Self:  # noqa: C901, PLR0912
        """Require citations, evidence identity, temporal evidence, display linkage."""
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
        cited_spans_by_claim: dict[UUID, set[UUID]] = {}
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
            spans = tuple(
                span
                for span in document.evidence_spans
                if span.span_id == citation.evidence_span_id
            )
            if not spans:
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
            cited_spans_by_claim.setdefault(citation.claim_id, set()).add(
                citation.evidence_span_id
            )
        if any(claim_id not in cited_spans_by_claim for claim_id in claim_ids):
            raise _error(
                "claim_missing_citation", "every claim must have at least one citation"
            )
        for claim in self.claims:
            if (
                claim.temporal_claim is not None
                and not set(claim.temporal_claim.evidence_span_ids)
                <= cited_spans_by_claim[claim.claim_id]
            ):
                raise _error(
                    "unknown_temporal_evidence",
                    "temporal evidence must be cited by its claim",
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
        self._enforce_display_blocks(claim_ids, uncertainty_ids)
        rendered_answer = self.rendered_answer()
        if self.answer != rendered_answer:
            raise _error(
                "displayed_answer_mismatch",
                "answer must equal deterministic validated display blocks",
            )
        return self

    def _enforce_display_blocks(
        self, claim_ids: tuple[UUID, ...], uncertainty_ids: tuple[UUID, ...]
    ) -> None:
        """Ensure display blocks reference each validated block exactly once."""
        displayed_claim_ids = tuple(
            block.claim_id
            for block in self.display_blocks
            if isinstance(block, ClaimDisplayBlock)
        )
        displayed_uncertainty_ids = tuple(
            block.uncertainty_id
            for block in self.display_blocks
            if isinstance(block, UncertaintyDisplayBlock)
        )
        if set(displayed_claim_ids) != set(claim_ids):
            raise _error(
                "unknown_display_claim", "display blocks must contain every claim once"
            )
        if set(displayed_uncertainty_ids) != set(uncertainty_ids):
            raise _error(
                "unknown_display_uncertainty",
                "display blocks must contain every uncertainty once",
            )
        if len(set(displayed_claim_ids)) != len(displayed_claim_ids):
            raise _error(
                "duplicate_display_claim", "claim display blocks must be unique"
            )
        if len(set(displayed_uncertainty_ids)) != len(displayed_uncertainty_ids):
            raise _error(
                "duplicate_display_uncertainty",
                "uncertainty display blocks must be unique",
            )
