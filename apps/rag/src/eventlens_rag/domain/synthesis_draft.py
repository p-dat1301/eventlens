"""Strict, runtime-validated provider draft (``synthesis_draft.v1``, frozen).

The provider may only propose claim text, references to span IDs it was given,
an optional temporal claim and model reason codes. It cannot supply titles,
URLs, hashes, versions, caller scope or free-form uncertainty prose: unknown
fields fail ``extra="forbid"``. Mechanical linkage against trusted sources is
checked later by the citation validator; this schema only enforces shape.
"""

# ruff: noqa: EM101

from typing import Annotated, Literal, LiteralString, Self
from uuid import UUID

from pydantic import Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from eventlens_contracts.base import ContractModel, NonEmptyText
from eventlens_rag.domain.synthesis_errors import (
    MalformedOutputReason,
    SynthesisMalformedOutputError,
)
from eventlens_rag.domain.synthesis_policy import ModelReasonCode
from eventlens_rag.domain.temporal import TemporalClaim

DRAFT_SCHEMA_VERSION = "synthesis_draft.v1"
MAX_DRAFT_CLAIMS = 20
MAX_SPANS_PER_CLAIM = 8
MAX_CLAIM_CHARS = 1_000
MAX_DRAFT_UNCERTAINTIES = 8

ClaimRef = Annotated[str, Field(pattern=r"^c[1-9][0-9]?$")]
ClaimText = Annotated[NonEmptyText, Field(max_length=MAX_CLAIM_CHARS)]


def _error(error_type: LiteralString, message: LiteralString) -> PydanticCustomError:
    return PydanticCustomError(error_type, message)


class DraftClaim(ContractModel):
    """One proposed claim citing span IDs from the payload actually sent."""

    claim_ref: ClaimRef
    text: ClaimText
    evidence_span_ids: Annotated[
        tuple[UUID, ...], Field(min_length=1, max_length=MAX_SPANS_PER_CLAIM)
    ]
    temporal_claim: TemporalClaim | None


class DraftUncertainty(ContractModel):
    """Reason code only; the server renders text from a versioned template."""

    reason_code: ModelReasonCode
    claim_ref: ClaimRef | None


class SynthesisDraft(ContractModel):
    """Whole provider draft; ``insufficient`` means a no-claims abstention."""

    schema_version: Literal["synthesis_draft.v1"]
    evidence_status: Literal["sufficient", "insufficient"]
    claims: Annotated[tuple[DraftClaim, ...], Field(max_length=MAX_DRAFT_CLAIMS)]
    uncertainties: Annotated[
        tuple[DraftUncertainty, ...], Field(max_length=MAX_DRAFT_UNCERTAINTIES)
    ]

    @model_validator(mode="after")
    def enforce_draft_shape(self) -> Self:
        """Unique refs, no dangling claim refs, consistent evidence status."""
        refs = [claim.claim_ref for claim in self.claims]
        if len(set(refs)) != len(refs):
            raise _error("duplicate_claim_ref", "claim_ref values must be unique")
        for claim in self.claims:
            ids = claim.evidence_span_ids
            if len(set(ids)) != len(ids):
                raise _error("duplicate_span_ref", "claim span ids must be unique")
        pairs = [(item.reason_code, item.claim_ref) for item in self.uncertainties]
        if len(set(pairs)) != len(pairs):
            raise _error("duplicate_uncertainty", "uncertainties must be unique")
        if any(
            item.claim_ref is not None and item.claim_ref not in refs
            for item in self.uncertainties
        ):
            raise _error("dangling_claim_ref", "uncertainty claim_ref must exist")
        if self.evidence_status == "sufficient" and not self.claims:
            raise _error("sufficient_without_claims", "sufficient needs claims")
        if self.evidence_status == "insufficient" and (
            self.claims
            or not any(
                item.reason_code == "insufficient_evidence"
                for item in self.uncertainties
            )
        ):
            raise _error(
                "insufficient_shape",
                "insufficient drafts need no claims and insufficient_evidence",
            )
        return self


def draft_json_schema() -> dict[str, object]:
    """JSON schema sent as the structured-output contract."""
    return SynthesisDraft.model_json_schema()


def parse_draft(content: str) -> SynthesisDraft:
    """Strictly parse provider content or raise a typed malformed-output error."""
    try:
        return SynthesisDraft.model_validate_json(content)
    except ValidationError as error:
        invalid_json = any(item["type"] == "json_invalid" for item in error.errors())
        reason = (
            MalformedOutputReason.INVALID_JSON
            if invalid_json
            else MalformedOutputReason.SCHEMA_VIOLATION
        )
        raise SynthesisMalformedOutputError(reason) from None
