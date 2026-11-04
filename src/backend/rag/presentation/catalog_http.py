"""Internal catalog API ``/internal/v1/catalog/*`` (service-to-service only).

Callers authenticate with ``Authorization: Bearer <service token>``; the
token is compared in constant time with the configured secret and never
logged. The routes must not be exposed by the public reverse proxy. Bodies
are the ``catalog.v1`` shared contracts; storage failures are ``503
catalog_unavailable``, never an empty page.
"""

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, cast
from uuid import UUID

import anyio.to_thread
from fastapi import APIRouter, Depends, Path, Query, Request

from backend.rag.application.use_cases.catalog import CatalogQueries
from backend.rag.domain.catalog import (
    DEFAULT_LIMIT,
    MAX_IDS_PER_FILTER,
    ArticleCard,
    ArticleDetail,
    ArticleGoneError,
    ArticleNotFoundError,
    ArticleSummaryView,
    ArticleVersionStaleError,
    CatalogFacets,
    CatalogFilters,
    CatalogRequestError,
    CatalogStorageError,
    FacetCount,
    LocationEntry,
    catalog_filters,
)
from backend.rag.presentation.api_errors import ApiError
from backend.shared.contracts.catalog import (
    ArticleCardV1,
    ArticleDetailV1,
    ArticleFacetsV1,
    ArticleListV1,
    ArticleSummaryV1,
    FacetCountV1,
    KeyPointV1,
    LabelProvenanceV1,
    LocationLabel,
    LocationListV1,
    LocationType,
    LocationV1,
    ProcessingStatus,
    SourceInfo,
    SourceListV1,
    SourceV1,
    SummaryGeneratorV1,
    SupportingSpanV1,
    TopicLabel,
    TopicListV1,
    TopicV1,
    VersionInfoV1,
)

PREFIX: Final = "/internal/v1/catalog"
TAXONOMY_VERSION: Final = "topic-taxonomy.v1"


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else value.astimezone(UTC)


def card_contract(card: ArticleCard) -> ArticleCardV1:
    """Domain card -> ``ArticleCardV1``."""
    return ArticleCardV1(
        article_id=card.article_id,
        article_version_id=card.article_version_id,
        upstream_version_id=card.upstream_version_id,
        title=card.title,
        source=SourceInfo(
            id=card.source.source_id, name=card.source.name, domain=card.source.domain
        ),
        url=card.canonical_url,
        published_at=_utc(card.published_at),
        topics=tuple(TopicLabel(id=t.topic_id, name=t.name) for t in card.topics),
        locations=tuple(
            LocationLabel(id=loc.location_id, name=loc.name, type=loc.location_type)
            for loc in card.locations
        ),
        summary_preview=card.summary_preview,
        processing_status=ProcessingStatus(
            metadata=card.metadata_status.value, summary=card.summary_status.value
        ),
    )


def detail_contract(detail: ArticleDetail) -> ArticleDetailV1:
    """Domain detail -> ``ArticleDetailV1`` (no internal fields)."""
    card = detail.card
    return ArticleDetailV1(
        schema_version="article_detail.v1",
        article=card_contract(card),
        content=detail.content,
        content_policy=detail.content_policy,
        ingested_at=_utc(detail.ingested_at),
        updated_at_source=_utc(detail.updated_at_source),
        language=detail.language,
        version=VersionInfoV1(
            article_version_id=card.article_version_id,
            upstream_version_id=card.upstream_version_id,
            revision_count=max(1, detail.revision_count),
            is_current=True,
        ),
        labels=tuple(
            LabelProvenanceV1(
                kind=label.kind,
                id=label.label_id,
                provenance=cast("Literal['enrichment', 'manual']", label.provenance),
                match_method=label.match_method,
                model_version=label.model_version,
                confidence=label.confidence,
                review_status=cast(
                    "Literal['unreviewed', 'approved', 'rejected']",
                    label.review_status,
                ),
            )
            for label in detail.labels
        ),
    )


def summary_contract(view: ArticleSummaryView) -> ArticleSummaryV1:
    """Domain summary state -> ``ArticleSummaryV1``."""
    generator = view.generator
    return ArticleSummaryV1(
        schema_version="article_summary.v1",
        article_id=view.article_id,
        article_version_id=view.article_version_id,
        status=view.status.value,
        source=view.source,
        text=view.text,
        key_points=tuple(
            KeyPointV1(
                text=point.text,
                supporting_spans=tuple(
                    SupportingSpanV1(
                        start_offset=s.start_offset,
                        end_offset=s.end_offset,
                        excerpt=s.excerpt,
                        span_fingerprint=s.span_fingerprint,
                    )
                    for s in point.spans
                ),
            )
            for point in view.key_points
        ),
        generator=None
        if generator is None
        else SummaryGeneratorV1(
            model_id=generator.model_id,
            model_version=generator.model_version,
            prompt_version=generator.prompt_version,
            schema_version=generator.schema_version,
            summary_policy_id=generator.summary_policy_id,
        ),
        failure_code=view.failure_code,
        created_at=_utc(view.created_at),
        publisher_summary=view.publisher_summary,
    )


def _facets(items: tuple[FacetCount, ...]) -> tuple[FacetCountV1, ...]:
    return tuple(
        FacetCountV1(
            id=f.facet_id,
            name=f.name,
            count=f.count,
            type=f.location_type,
            domain=f.domain,
        )
        for f in items
    )


def facets_contract(facets: CatalogFacets) -> ArticleFacetsV1:
    """Domain facets -> ``ArticleFacetsV1``."""
    return ArticleFacetsV1(
        schema_version="article_facets.v1",
        semantics="all_filters_applied",
        total=facets.total,
        topics=_facets(facets.topics),
        topics_unclassified=facets.topics_unclassified,
        locations=_facets(facets.locations),
        locations_unknown=facets.locations_unknown,
        sources=_facets(facets.sources),
        as_of=facets.as_of.astimezone(UTC),
    )


def location_contract(entry: LocationEntry) -> LocationV1:
    """Gazetteer entry -> ``LocationV1``."""
    return LocationV1(
        id=entry.location_id,
        name=entry.name,
        type=cast("LocationType", entry.location_type),
        parent_id=entry.parent_id,
        valid_until=entry.valid_until,
        aliases=entry.aliases,
    )


CATALOG_ERRORS: Final[tuple[tuple[type[Exception], int], ...]] = (
    (CatalogRequestError, 400),
    (ArticleNotFoundError, 404),
    (ArticleGoneError, 410),
    (ArticleVersionStaleError, 409),
    (CatalogStorageError, 503),
)


def api_error_of(error: Exception) -> ApiError | None:
    """Map a typed catalog error to its stable public form."""
    for kind, status in CATALOG_ERRORS:
        if isinstance(error, kind):
            code = cast("str", getattr(error, "code", "internal_error"))
            reason = (
                error.current_version_id
                if isinstance(error, ArticleVersionStaleError)
                else None
            )
            return ApiError(status, code, reason)
    return None


async def run_catalog[T](call: Callable[[], T]) -> T:
    """Run a blocking catalog read off the event loop with typed errors."""
    try:
        return await anyio.to_thread.run_sync(call)
    except (
        CatalogRequestError,
        ArticleNotFoundError,
        ArticleGoneError,
        ArticleVersionStaleError,
        CatalogStorageError,
    ) as error:
        mapped = api_error_of(error)
        assert mapped is not None  # noqa: S101
        raise mapped from None


def filters_of(  # noqa: PLR0913 - one argument per public filter
    *,
    q: str | None,
    topic_ids: list[str] | None,
    location_ids: list[str] | None,
    source_ids: list[UUID] | None,
    published_after: datetime | None,
    published_before: datetime | None,
    include_descendants: bool,
) -> CatalogFilters:
    """Shared query-parameter parsing (400 ``CatalogRequestError`` codes)."""
    try:
        return catalog_filters(
            q=q,
            topic_ids=tuple(topic_ids or ()),
            location_ids=tuple(location_ids or ()),
            source_ids=tuple(source_ids or ()),
            published_after=published_after,
            published_before=published_before,
            include_descendants=include_descendants,
        )
    except CatalogRequestError as error:
        raise ApiError(400, error.code) from None


IdList = Annotated[list[str] | None, Query(max_length=MAX_IDS_PER_FILTER)]
UuidList = Annotated[list[UUID] | None, Query(max_length=MAX_IDS_PER_FILTER)]


@dataclass(frozen=True, slots=True)
class FilterParams:
    """Dependency bundling the catalog query parameters."""

    filters: CatalogFilters


def filter_params(  # noqa: PLR0913, PLR0917 - FastAPI query parameters
    q: Annotated[str | None, Query(max_length=400)] = None,
    topic_ids: IdList = None,
    location_ids: IdList = None,
    source_ids: UuidList = None,
    published_after: datetime | None = None,
    published_before: datetime | None = None,
    include_descendants: bool = True,  # noqa: FBT001, FBT002
) -> FilterParams:
    """Parse the repeated-parameter filters."""
    return FilterParams(
        filters_of(
            q=q,
            topic_ids=topic_ids,
            location_ids=location_ids,
            source_ids=source_ids,
            published_after=published_after,
            published_before=published_before,
            include_descendants=include_descendants,
        )
    )


@dataclass(frozen=True, slots=True)
class ServiceAuth:
    """Bearer service-token check for internal routes."""

    token: str | None

    def __call__(self, request: Request) -> str:
        """Verified service principal or ``401``/``503``."""
        if not self.token:
            raise ApiError(503, "internal_auth_not_configured")
        header = request.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            presented.encode(), self.token.encode()
        ):
            raise ApiError(401, "service_unauthenticated")
        return "crud-api"


def build_catalog_router(catalog: CatalogQueries, auth: ServiceAuth) -> APIRouter:
    """Internal catalog routes; one semantics shared with chat scopes."""
    router = APIRouter(prefix=PREFIX, dependencies=[Depends(auth)])

    @router.get("/articles")
    async def articles(
        params: Annotated[FilterParams, Depends(filter_params)],
        sort: Annotated[
            Literal["latest", "oldest", "relevance"] | None, Query()
        ] = None,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = DEFAULT_LIMIT,
    ) -> ArticleListV1:
        page = await run_catalog(
            lambda: catalog.list_articles(
                params.filters, sort=sort, limit=limit, cursor=cursor
            )
        )
        return ArticleListV1(
            schema_version="article_list.v1",
            items=tuple(card_contract(card) for card in page.items),
            next_cursor=page.next_cursor,
            has_more=page.has_more,
            as_of=page.as_of.astimezone(UTC),
            search_mode=page.search_mode,
        )

    @router.get("/articles/{article_id}")
    async def article(
        article_id: Annotated[UUID, Path()],
        article_version_id: Annotated[
            str | None, Query(pattern=r"^sha256:[0-9a-f]{64}$")
        ] = None,
    ) -> ArticleDetailV1:
        detail = await run_catalog(
            lambda: catalog.article(article_id, article_version_id)
        )
        return detail_contract(detail)

    @router.get("/articles/{article_id}/summary")
    async def summary(article_id: Annotated[UUID, Path()]) -> ArticleSummaryV1:
        view = await run_catalog(lambda: catalog.summary(article_id))
        return summary_contract(view)

    @router.get("/facets")
    async def facets(
        params: Annotated[FilterParams, Depends(filter_params)],
    ) -> ArticleFacetsV1:
        found = await run_catalog(lambda: catalog.facets(params.filters))
        return facets_contract(found)

    @router.get("/topics")
    async def topics() -> TopicListV1:
        found = await run_catalog(catalog.topics)
        return TopicListV1(
            schema_version="topic_list.v1",
            taxonomy_version=found[0].taxonomy_version if found else TAXONOMY_VERSION,
            items=tuple(
                TopicV1(id=t.topic_id, name=t.name, name_en=t.name_en) for t in found
            ),
        )

    @router.get("/locations")
    async def locations(
        parent_id: Annotated[str | None, Query(pattern=r"^[a-z0-9-]{1,80}$")] = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> LocationListV1:
        found = await run_catalog(lambda: catalog.locations(parent_id, q, limit))
        return LocationListV1(
            schema_version="location_list.v1",
            gazetteer_version=found[0].gazetteer_version if found else None,
            items=tuple(location_contract(entry) for entry in found),
        )

    @router.get("/sources")
    async def sources() -> SourceListV1:
        found = await run_catalog(catalog.sources)
        return SourceListV1(
            schema_version="source_list.v1",
            items=tuple(
                SourceV1(
                    id=s.source_id,
                    name=s.name,
                    domain=s.domain,
                    article_count=s.article_count,
                )
                for s in found
            ),
        )

    return router
