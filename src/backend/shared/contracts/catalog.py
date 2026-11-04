"""Article catalog wire contracts (``*.v1``) shared by rag-api and crud-api.

rag-api produces them on ``/internal/v1/catalog/*``; crud-api validates them
strictly (unknown fields are a contract mismatch) and serves them unchanged
on ``/api/v1``. Timestamps are UTC. ``article_version_id`` is the RAG
revision key (``sha256:`` of the NFC content); ``upstream_version_id`` is the
data team's ``article_version_id`` when the revision came from a handoff.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from backend.shared.contracts.base import (
    ContractModel,
    NonNegativeInt,
    PrefixedSha256,
    UtcDatetime,
)

MetadataState = Literal["ready", "pending", "failed", "unavailable"]
SummaryState = Literal["ready", "pending", "failed", "unavailable"]
LocationType = Literal["country", "municipality", "province", "former_province"]


class TopicLabel(ContractModel):
    """Topic of an article."""

    id: str
    name: str


class LocationLabel(ContractModel):
    """Location the article content is about (not the newsroom's location)."""

    id: str
    name: str
    type: str


class SourceInfo(ContractModel):
    """Publisher."""

    id: UUID
    name: str
    domain: str


class ProcessingStatus(ContractModel):
    """Derived-data states of the current revision."""

    metadata: MetadataState
    summary: SummaryState


class ArticleCardV1(ContractModel):
    """One current article (never a chunk); no full text."""

    article_id: UUID
    article_version_id: PrefixedSha256
    upstream_version_id: UUID | None
    title: str
    source: SourceInfo
    url: str
    published_at: UtcDatetime | None
    topics: tuple[TopicLabel, ...]
    locations: tuple[LocationLabel, ...]
    summary_preview: str | None
    processing_status: ProcessingStatus


class ArticleListV1(ContractModel):
    """Keyset page; ``next_cursor`` is opaque and bound to filters + sort."""

    schema_version: Literal["article_list.v1"]
    items: tuple[ArticleCardV1, ...]
    next_cursor: str | None
    has_more: bool
    as_of: UtcDatetime
    search_mode: Literal["none", "lexical"]


class LabelProvenanceV1(ContractModel):
    """Provenance of one label of the current revision."""

    kind: Literal["topic", "location"]
    id: str
    provenance: Literal["enrichment", "manual"]
    match_method: str
    model_version: str | None
    confidence: float | None
    review_status: Literal["unreviewed", "approved", "rejected"]


class VersionInfoV1(ContractModel):
    """Version state of the served revision."""

    article_version_id: PrefixedSha256
    upstream_version_id: UUID | None
    revision_count: Annotated[int, Field(ge=1)]
    is_current: Literal[True]


class ArticleDetailV1(ContractModel):
    """Current revision; ``content`` is the normalized plain text when allowed."""

    schema_version: Literal["article_detail.v1"]
    article: ArticleCardV1
    content: str | None
    content_policy: Literal["full", "withheld"]
    ingested_at: UtcDatetime | None
    updated_at_source: UtcDatetime | None
    language: str | None
    version: VersionInfoV1
    labels: tuple[LabelProvenanceV1, ...]


class SupportingSpanV1(ContractModel):
    """Code-point offsets into the NFC content of ``article_version_id``."""

    start_offset: NonNegativeInt
    end_offset: NonNegativeInt
    excerpt: str
    span_fingerprint: PrefixedSha256


class KeyPointV1(ContractModel):
    """Key point with its supporting spans."""

    text: str
    supporting_spans: tuple[SupportingSpanV1, ...]


class SummaryGeneratorV1(ContractModel):
    """Model/prompt/schema identity of a stored AI summary."""

    model_id: str
    model_version: str
    prompt_version: str
    schema_version: str
    summary_policy_id: str


class ArticleSummaryV1(ContractModel):
    """Stored summary state; reading it never calls a model."""

    schema_version: Literal["article_summary.v1"]
    article_id: UUID
    article_version_id: PrefixedSha256
    status: SummaryState
    source: Literal["ai"]
    text: str | None
    key_points: tuple[KeyPointV1, ...]
    generator: SummaryGeneratorV1 | None
    failure_code: str | None
    created_at: UtcDatetime | None
    publisher_summary: str | None


class FacetCountV1(ContractModel):
    """Distinct current articles with one label under all selected filters."""

    id: str
    name: str
    count: NonNegativeInt
    type: str | None = None
    domain: str | None = None


class ArticleFacetsV1(ContractModel):
    """Facet counts; location counts include descendants (gazetteer rollup)."""

    schema_version: Literal["article_facets.v1"]
    semantics: Literal["all_filters_applied"]
    total: NonNegativeInt
    topics: tuple[FacetCountV1, ...]
    topics_unclassified: NonNegativeInt
    locations: tuple[FacetCountV1, ...]
    locations_unknown: NonNegativeInt
    sources: tuple[FacetCountV1, ...]
    as_of: UtcDatetime


class TopicV1(ContractModel):
    """Taxonomy topic."""

    id: str
    name: str
    name_en: str


class TopicListV1(ContractModel):
    """Topic taxonomy."""

    schema_version: Literal["topic_list.v1"]
    taxonomy_version: str
    items: tuple[TopicV1, ...]


class LocationV1(ContractModel):
    """Gazetteer unit."""

    id: str
    name: str
    type: LocationType
    parent_id: str | None
    valid_until: str | None
    aliases: tuple[str, ...]


class LocationListV1(ContractModel):
    """Gazetteer lookup result."""

    schema_version: Literal["location_list.v1"]
    gazetteer_version: str | None
    items: tuple[LocationV1, ...]


class SourceV1(ContractModel):
    """Publisher with its number of current articles."""

    id: UUID
    name: str
    domain: str
    article_count: NonNegativeInt


class SourceListV1(ContractModel):
    """Publishers."""

    schema_version: Literal["source_list.v1"]
    items: tuple[SourceV1, ...]


class ApiErrorDetail(ContractModel):
    """Stable code, optional enum reason, request correlation id."""

    code: str
    reason: str | None = None
    request_id: str | None = None


class ApiErrorV1(ContractModel):
    """Error envelope of every ``/api/v1`` and ``/internal/v1`` route."""

    schema_version: Literal["api_error.v1"]
    error: ApiErrorDetail
