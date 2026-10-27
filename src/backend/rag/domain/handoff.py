"""Data-team handoff batch contract ``eventlens-rag-handoff-v1``.

A batch is ``articles.jsonl`` (one article version per line) plus
``manifest.json``. The handoff is not an ``ArticleBundle``: it carries no raw
fetch, duplicate or media metadata, and none is invented here. A record maps to
an ``ArticleDocument`` directly; upstream identity (``article_version_id``,
upstream ``content_hash``, parser/normalizer versions) is kept as
``SourceProvenance`` next to the RAG revision instead of being folded into it.

The upstream ``content_hash`` is opaque: its input/normalization is defined by
the parser team and differs from RAG ``nfc.v1``. RAG recomputes its own hash and
records both, so a mismatch between the two is expected and never an error.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from unicodedata import normalize
from uuid import UUID

from pydantic import Field, HttpUrl

from backend.rag.domain.article_document import (
    NORMALIZATION_VERSION,
    ArticleDocument,
    normalized_content_hash,
)
from backend.rag.domain.article_ingest import IngestClock
from backend.shared.contracts.base import (
    CanonicalDomain,
    ContractModel,
    NonEmptyText,
    NonNegativeInt,
    NonNegativeInt32,
    Sha256,
    UtcDatetime,
)

HANDOFF_SCHEMA_VERSION = "eventlens-rag-handoff-v1"


class HandoffArticle(ContractModel):
    """One exported article version, exactly as the data team sends it."""

    article_id: UUID
    article_version_id: UUID
    canonical_url: HttpUrl
    title: NonEmptyText
    content: NonEmptyText
    source_id: UUID
    source_name: NonEmptyText
    source_domain: CanonicalDomain
    published_at: UtcDatetime | None
    updated_at_source: UtcDatetime | None
    fetched_at: UtcDatetime
    language: NonEmptyText
    word_count: NonNegativeInt32
    content_hash: Sha256
    normalization_version: NonEmptyText
    parse_version: NonEmptyText
    quality_status: Literal["passed", "warning"]
    quality_flags: tuple[str, ...]
    is_current: bool


class HandoffSelection(ContractModel):
    """How the exporter sampled the batch."""

    current_only: bool
    quality_status: tuple[Literal["passed", "warning"], ...]
    sampling: NonEmptyText
    seed: NonEmptyText


class HandoffManifest(ContractModel):
    """Batch manifest; every count is re-derived and compared by the validator."""

    schema_version: Literal["eventlens-rag-handoff-v1"]
    export_id: NonEmptyText
    exported_at: UtcDatetime
    articles_object: NonEmptyText
    articles_sha256: Sha256
    articles_bytes: NonNegativeInt
    record_count: NonNegativeInt
    language_counts: dict[str, NonNegativeInt]
    source_counts: dict[str, NonNegativeInt]
    parse_version_counts: dict[str, NonNegativeInt]
    quality_status_counts: dict[str, NonNegativeInt]
    missing_published_at_count: NonNegativeInt
    published_at_min: UtcDatetime | None
    published_at_max: UtcDatetime | None
    fetched_at_min: UtcDatetime
    fetched_at_max: UtcDatetime
    selection: HandoffSelection
    extra_counts: dict[str, NonNegativeInt] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """Upstream identity of one RAG revision; the join key for citations.

    ``(article_id, rag_content_hash)`` is the RAG revision key; the upstream
    ``article_version_id`` and ``upstream_content_hash`` are kept verbatim.
    """

    article_id: UUID
    rag_content_hash: str
    article_version_id: UUID
    upstream_content_hash: str
    upstream_normalization_version: str
    parse_version: str
    quality_status: str
    export_id: str
    fetched_at: datetime
    updated_at_source: datetime | None
    language: str
    word_count: int


def document_from_handoff(
    record: HandoffArticle, clock: IngestClock
) -> ArticleDocument:
    """Map a handoff record to an ArticleDocument without inventing fields.

    ``raw_content`` is the exported content byte-for-byte (as decoded text);
    ``normalized_content`` is its NFC form, so citation offsets are Unicode code
    points of the NFC text. ``source_published_at`` stays ``None`` when the
    publisher time is unknown; ``fetched_at`` is never substituted.
    """
    normalized = normalize("NFC", record.content)
    digest = normalized_content_hash(normalized)
    return ArticleDocument(
        schema_version="article_document.v1",
        article_id=record.article_id,
        source_id=record.source_id,
        title=record.title,
        raw_content=record.content,
        normalization_version=NORMALIZATION_VERSION,
        normalized_content=normalized,
        canonical_url=record.canonical_url,
        source_name=record.source_name,
        source_domain=record.source_domain,
        source_published_at=record.published_at,
        ingested_at=clock.ingested_at,
        processed_at=clock.processed_at,
        content_hash=digest,
        content_fingerprint=digest,
        visibility="internal",
        evidence_spans=(),
    )


def provenance_of(
    record: HandoffArticle, document: ArticleDocument, export_id: str
) -> SourceProvenance:
    """Upstream identity of ``document`` (which must come from ``record``)."""
    return SourceProvenance(
        article_id=document.article_id,
        rag_content_hash=document.content_hash,
        article_version_id=record.article_version_id,
        upstream_content_hash=record.content_hash,
        upstream_normalization_version=record.normalization_version,
        parse_version=record.parse_version,
        quality_status=record.quality_status,
        export_id=export_id,
        fetched_at=record.fetched_at,
        updated_at_source=record.updated_at_source,
        language=record.language,
        word_count=record.word_count,
    )
