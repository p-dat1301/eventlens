"""Article catalog: filter semantics, sort keys, cursors and read models.

This module is the single definition of catalog filter semantics; the
PostgreSQL adapter implements exactly these rules and conversation scopes of
type ``filtered`` reuse them (never a page of results).

* IDs of one kind are OR-ed, kinds are AND-ed.
* ``[published_after, published_before)`` on the publisher time; bounds must
  carry a timezone and are compared in UTC. An article without publisher time
  never matches a time bound; ``fetched_at`` is never substituted.
* ``include_descendants`` widens each location to its gazetteer subtree.
* ``q`` is a lexical (PostgreSQL ``simple`` websearch) match on title + text.
* Sort keys always end in ``article_id`` so pagination is stable.
"""

# ruff: noqa: EM101

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar, Final, Literal, cast
from uuid import UUID

MAX_QUERY_CHARS: Final = 200
MAX_IDS_PER_FILTER: Final = 20
DEFAULT_LIMIT: Final = 20
MAX_LIMIT: Final = 50
MAX_CURSOR_CHARS: Final = 1024
CURSOR_VERSION: Final = 1
SUMMARY_PREVIEW_CHARS: Final = 240
TOPIC_ID: Final = re.compile(r"^[a-z_]{1,40}$")
LOCATION_ID: Final = re.compile(r"^[a-z0-9-]{1,80}$")


class CatalogSort(StrEnum):
    """Catalog orderings; every key ends in ``article_id``."""

    LATEST = "latest"  # published_at DESC NULLS LAST, article_id ASC
    OLDEST = "oldest"  # published_at ASC NULLS LAST, article_id ASC
    RELEVANCE = "relevance"  # rank DESC, published_at DESC NULLS LAST, id ASC


class MetadataStatus(StrEnum):
    """Topic/location labelling state of the current revision."""

    READY = "ready"
    PENDING = "pending"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class SummaryStatus(StrEnum):
    """AI summary state of the current revision."""

    READY = "ready"
    PENDING = "pending"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class CatalogRequestError(ValueError):
    """Rejected catalog request; ``code`` is stable, the message is fixed."""

    def __init__(self, code: str) -> None:
        """Keep the stable code only."""
        super().__init__("catalog request rejected")
        self.code: str = code


class ArticleNotFoundError(LookupError):
    """No such article (or never visible to this caller)."""

    code: ClassVar[str] = "article_not_found"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("article not found")


class ArticleGoneError(LookupError):
    """The article is tombstoned; it is never served as active."""

    code: ClassVar[str] = "article_gone"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("article was removed")


class ArticleVersionStaleError(LookupError):
    """A requested version is no longer the current revision."""

    code: ClassVar[str] = "article_version_stale"

    def __init__(self, current_version_id: str) -> None:
        """Keep the current version id (safe to return)."""
        super().__init__("article version is not current")
        self.current_version_id: str = current_version_id


class CatalogStorageError(RuntimeError):
    """Catalog storage unavailable or failed; never an empty result."""

    code: ClassVar[str] = "catalog_unavailable"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("catalog storage unavailable")


def _utc(value: datetime | None, code: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise CatalogRequestError(code)
    return value.astimezone(UTC)


def _unique[T](values: tuple[T, ...], code: str) -> tuple[T, ...]:
    if len(values) > MAX_IDS_PER_FILTER:
        raise CatalogRequestError(code)
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True, slots=True)
class CatalogFilters:
    """Validated, normalized filters (construct with ``catalog_filters``)."""

    q: str | None
    topic_ids: tuple[str, ...]
    location_ids: tuple[str, ...]
    source_ids: tuple[UUID, ...]
    published_after: datetime | None
    published_before: datetime | None
    include_descendants: bool

    @property
    def empty_range(self) -> bool:
        """``after == before`` selects nothing (half-open interval)."""
        return (
            self.published_after is not None
            and self.published_after == self.published_before
        )

    def canonical(self) -> dict[str, object]:
        """Order-independent JSON form (scope definitions, cursor binding)."""
        return {
            "q": self.q,
            "topic_ids": sorted(self.topic_ids),
            "location_ids": sorted(self.location_ids),
            "source_ids": sorted(str(s) for s in self.source_ids),
            "published_after": _iso(self.published_after),
            "published_before": _iso(self.published_before),
            "include_descendants": self.include_descendants,
        }

    def digest(self) -> str:
        """SHA-256 of the canonical form."""
        text = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def catalog_filters(  # noqa: PLR0913 - one argument per public filter
    *,
    q: str | None = None,
    topic_ids: tuple[str, ...] = (),
    location_ids: tuple[str, ...] = (),
    source_ids: tuple[UUID, ...] = (),
    published_after: datetime | None = None,
    published_before: datetime | None = None,
    include_descendants: bool = True,
) -> CatalogFilters:
    """Validate and normalize; raises ``CatalogRequestError``."""
    text = None if q is None else " ".join(q.split())
    if text is not None and len(text) > MAX_QUERY_CHARS:
        raise CatalogRequestError("query_too_long")
    topics = _unique(topic_ids, "too_many_topic_ids")
    locations = _unique(location_ids, "too_many_location_ids")
    if not all(TOPIC_ID.match(t) for t in topics):
        raise CatalogRequestError("invalid_topic_id")
    if not all(LOCATION_ID.match(loc) for loc in locations):
        raise CatalogRequestError("invalid_location_id")
    after = _utc(published_after, "timezone_required")
    before = _utc(published_before, "timezone_required")
    if after is not None and before is not None and after > before:
        raise CatalogRequestError("invalid_publication_range")
    return CatalogFilters(
        q=text or None,
        topic_ids=topics,
        location_ids=locations,
        source_ids=_unique(source_ids, "too_many_source_ids"),
        published_after=after,
        published_before=before,
        include_descendants=include_descendants,
    )


@dataclass(frozen=True, slots=True)
class CursorPosition:
    """Keyset position after the last returned item."""

    published_at: datetime | None
    article_id: UUID
    rank: float | None


@dataclass(frozen=True, slots=True)
class CursorCodec:
    """Opaque HMAC-signed cursor bound to filter digest, sort and limit."""

    secret: bytes

    def encode(
        self, filters: CatalogFilters, sort: CatalogSort, position: CursorPosition
    ) -> str:
        """Serialize ``position`` for this exact filter/sort."""
        body = json.dumps(
            {
                "v": CURSOR_VERSION,
                "f": filters.digest(),
                "s": sort.value,
                "p": _iso(position.published_at),
                "a": str(position.article_id),
                "r": None if position.rank is None else repr(position.rank),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        tag = hmac.new(self.secret, body, hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(tag + body).decode().rstrip("=")

    def decode(
        self, token: str, filters: CatalogFilters, sort: CatalogSort
    ) -> CursorPosition:
        """Verify and parse; ``invalid_cursor`` or ``cursor_mismatch``."""
        if not 0 < len(token) <= MAX_CURSOR_CHARS:
            raise CatalogRequestError("invalid_cursor")
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        except ValueError:
            raise CatalogRequestError("invalid_cursor") from None
        tag, body = raw[:16], raw[16:]
        expected = hmac.new(self.secret, body, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, expected):
            raise CatalogRequestError("invalid_cursor")
        try:
            data = cast("dict[str, object]", json.loads(body))
            if data.get("v") != CURSOR_VERSION:
                raise CatalogRequestError("invalid_cursor")
            if data.get("f") != filters.digest() or data.get("s") != sort.value:
                raise CatalogRequestError("cursor_mismatch")
            published = data.get("p")
            rank = data.get("r")
            return CursorPosition(
                published_at=None
                if published is None
                else datetime.fromisoformat(str(published)),
                article_id=UUID(str(data["a"])),
                rank=None if rank is None else float(str(rank)),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, CatalogRequestError):
                raise
            raise CatalogRequestError("invalid_cursor") from None


@dataclass(frozen=True, slots=True)
class TopicRef:
    """Topic label of an article."""

    topic_id: str
    name: str


@dataclass(frozen=True, slots=True)
class LocationRef:
    """Location mentioned by an article (not the newsroom's location)."""

    location_id: str
    name: str
    location_type: str


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Publisher of an article."""

    source_id: UUID
    name: str
    domain: str


@dataclass(frozen=True, slots=True)
class ArticleCard:
    """One current article (never a chunk) for lists."""

    article_id: UUID
    article_version_id: str
    upstream_version_id: UUID | None
    title: str
    source: SourceRef
    canonical_url: str
    published_at: datetime | None
    topics: tuple[TopicRef, ...]
    locations: tuple[LocationRef, ...]
    summary_preview: str | None
    metadata_status: MetadataStatus
    summary_status: SummaryStatus


@dataclass(frozen=True, slots=True)
class CatalogRow:
    """Store row: a card plus its sort keys."""

    card: ArticleCard
    rank: float | None


@dataclass(frozen=True, slots=True)
class ArticlePage:
    """One page of current articles."""

    items: tuple[ArticleCard, ...]
    next_cursor: str | None
    has_more: bool
    as_of: datetime
    search_mode: Literal["none", "lexical"]


@dataclass(frozen=True, slots=True)
class LabelProvenance:
    """Where one label of the current revision came from."""

    kind: Literal["topic", "location"]
    label_id: str
    provenance: str
    match_method: str
    model_version: str | None
    confidence: float | None
    review_status: str


@dataclass(frozen=True, slots=True)
class ArticleDetail:
    """Current revision with provenance; content only when policy allows."""

    card: ArticleCard
    content: str | None
    content_policy: Literal["full", "withheld"]
    ingested_at: datetime | None
    updated_at_source: datetime | None
    language: str | None
    revision_count: int
    labels: tuple[LabelProvenance, ...]


@dataclass(frozen=True, slots=True)
class SummaryKeyPoint:
    """One key point and the exact spans of the revision supporting it."""

    text: str
    spans: tuple["SupportingSpan", ...]


@dataclass(frozen=True, slots=True)
class SupportingSpan:
    """Unicode code-point offsets into the NFC content of one revision."""

    start_offset: int
    end_offset: int
    excerpt: str
    span_fingerprint: str


@dataclass(frozen=True, slots=True)
class SummaryGenerator:
    """Identity of the model call that produced a summary."""

    model_id: str
    model_version: str
    prompt_version: str
    schema_version: str
    summary_policy_id: str


@dataclass(frozen=True, slots=True)
class ArticleSummaryView:
    """Summary state of the current revision; read-only, never calls a model."""

    article_id: UUID
    article_version_id: str
    status: SummaryStatus
    source: Literal["ai"]
    text: str | None
    key_points: tuple[SummaryKeyPoint, ...]
    generator: SummaryGenerator | None
    failure_code: str | None
    created_at: datetime | None
    publisher_summary: str | None


@dataclass(frozen=True, slots=True)
class FacetCount:
    """Distinct current articles carrying one label within the filters."""

    facet_id: str
    name: str
    count: int
    location_type: str | None = None
    domain: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogFacets:
    """Counts after applying every selected filter (not self-excluding)."""

    total: int
    topics: tuple[FacetCount, ...]
    topics_unclassified: int
    locations: tuple[FacetCount, ...]
    locations_unknown: int
    sources: tuple[FacetCount, ...]
    as_of: datetime


@dataclass(frozen=True, slots=True)
class TopicEntry:
    """Taxonomy topic."""

    topic_id: str
    name: str
    name_en: str
    taxonomy_version: str


@dataclass(frozen=True, slots=True)
class LocationEntry:
    """Gazetteer unit."""

    location_id: str
    name: str
    location_type: str
    parent_id: str | None
    valid_until: str | None
    aliases: tuple[str, ...]
    gazetteer_version: str


@dataclass(frozen=True, slots=True)
class SourceEntry:
    """Publisher with its number of current articles."""

    source_id: UUID
    name: str
    domain: str
    article_count: int
