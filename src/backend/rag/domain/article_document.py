"""Normalized immutable searchable article contract."""

# ruff: noqa: EM101

from hashlib import sha256
from typing import Literal, LiteralString, Self
from unicodedata import normalize
from uuid import UUID

from pydantic import HttpUrl, model_validator
from pydantic_core import PydanticCustomError

from backend.shared.contracts.base import (
    CanonicalDomain,
    ContractModel,
    NonEmptyText,
    PrefixedSha256,
    UtcDatetime,
)
from backend.shared.contracts.evidence import EvidenceSpan

NORMALIZATION_VERSION = "nfc.v1"


def _error(error_type: LiteralString, message: LiteralString) -> PydanticCustomError:
    return PydanticCustomError(error_type, message)


def normalized_content_hash(content: str) -> str:
    """Return stable SHA-256 identity for NFC searchable content."""
    return f"sha256:{sha256(content.encode()).hexdigest()}"


class ArticleDocument(ContractModel):
    """Searchable NFC content plus immutable original-source provenance."""

    schema_version: Literal["article_document.v1"]
    article_id: UUID
    source_id: UUID
    title: NonEmptyText
    raw_content: NonEmptyText
    normalization_version: Literal["nfc.v1"]
    normalized_content: NonEmptyText
    canonical_url: HttpUrl
    source_name: NonEmptyText
    source_domain: CanonicalDomain
    source_published_at: UtcDatetime | None
    ingested_at: UtcDatetime
    processed_at: UtcDatetime
    content_hash: PrefixedSha256
    content_fingerprint: PrefixedSha256
    visibility: Literal["internal"]
    evidence_spans: tuple[EvidenceSpan, ...]

    @model_validator(mode="after")
    def enforce_normalized_identity_and_evidence(self) -> Self:
        """Bind spans and immutable identities to exact NFC content."""
        expected_content = normalize("NFC", self.raw_content)
        expected_hash = normalized_content_hash(expected_content)
        if self.normalized_content != expected_content:
            raise _error(
                "normalized_content_mismatch",
                "normalized_content must be NFC raw_content",
            )
        if (
            self.content_hash != expected_hash
            or self.content_fingerprint != expected_hash
        ):
            raise _error(
                "normalized_content_identity_mismatch",
                "content hash and fingerprint must match normalized_content",
            )
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
