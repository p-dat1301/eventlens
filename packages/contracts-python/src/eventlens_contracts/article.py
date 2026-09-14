"""Article v1 canonical contract."""

from typing import Literal
from uuid import UUID

from pydantic import HttpUrl

from eventlens_contracts.base import (
    CanonicalDomain,
    ContractModel,
    HttpStatus,
    NonEmptyText,
    NonNegativeInt32,
    NonNegativeInt64,
    PrefixedSha256,
    StorageUri,
    UtcDatetime,
)


class ArticleSource(ContractModel):
    """Publisher identity embedded in canonical article."""

    name: NonEmptyText
    domain: CanonicalDomain
    authority_tier: Literal["official", "tier1", "tier2", "unknown"]


class ArticleDuplicate(ContractModel):
    """Deduplication decision embedded in canonical article."""

    is_duplicate: bool
    canonical_article_id: UUID | None
    duplicate_type: str | None
    score: float | None
    method: str | None


class ArticleRaw(ContractModel):
    """Raw evidence lineage for canonical article."""

    article_raw_id: UUID
    storage_uri: StorageUri
    body_hash: PrefixedSha256
    bytes: NonNegativeInt64
    http_status: HttpStatus
    fetched_at: UtcDatetime
    crawl_run_id: UUID


class ArticleProcessing(ContractModel):
    """Extraction provenance and quality decision."""

    parse_version: NonEmptyText
    quality_status: Literal["passed", "flagged"]
    quality_flags: tuple[str, ...]
    extraction_methods: dict[str, str]
    extracted_at: UtcDatetime


class Article(ContractModel):
    """Normalized article payload version 1."""

    schema_version: Literal["article.v1"]
    article_id: UUID
    source_id: UUID
    source: ArticleSource
    canonical_url: HttpUrl
    title: NonEmptyText
    content: NonEmptyText
    summary_source: str | None = None
    author: str | None = None
    section: str | None = None
    tags: tuple[str, ...]
    published_at: UtcDatetime | None = None
    updated_at_source: UtcDatetime | None = None
    lead_image_url: HttpUrl | None = None
    lead_image_storage_uri: StorageUri | None = None
    media_count: NonNegativeInt32
    language: NonEmptyText
    word_count: NonNegativeInt32
    content_hash: PrefixedSha256
    duplicate: ArticleDuplicate
    raw: ArticleRaw
    processing: ArticleProcessing
