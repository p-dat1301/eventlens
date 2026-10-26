"""Immutable normalized-content evidence spans."""

# ruff: noqa: EM101

from hashlib import sha256
from typing import Annotated, Literal, LiteralString, Self
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from backend.shared.contracts.base import (
    ContractModel,
    NonEmptyText,
    NonNegativeInt32,
    PrefixedSha256,
)


def _error(error_type: LiteralString, message: LiteralString) -> PydanticCustomError:
    return PydanticCustomError(error_type, message)


class EvidenceSpan(ContractModel):
    """Unicode code-point half-open range in one normalized content revision."""

    span_id: UUID
    article_id: UUID
    content_hash: PrefixedSha256
    start_offset: NonNegativeInt32
    end_offset: Annotated[int, Field(ge=1, le=2_147_483_647)]
    excerpt: NonEmptyText
    span_fingerprint: PrefixedSha256
    offset_basis: Literal["unicode_codepoint"]

    @model_validator(mode="after")
    def enforce_offset_order(self) -> Self:
        """Reject empty or reversed source ranges."""
        if self.end_offset <= self.start_offset:
            raise _error(
                "invalid_evidence_offsets", "end_offset must exceed start_offset"
            )
        expected_fingerprint = f"sha256:{sha256(self.excerpt.encode()).hexdigest()}"
        if self.span_fingerprint != expected_fingerprint:
            raise _error(
                "evidence_span_fingerprint_mismatch",
                "span fingerprint must match excerpt",
            )
        return self
