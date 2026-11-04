"""``/api/v1/articles*`` and ``/api/v1/article-facets`` (crud-api).

Multi-value filters are repeated query parameters
(``?topic_ids=a&topic_ids=b``): OR within one kind, AND across kinds. Dates
are ISO-8601 with an explicit offset; ``[published_after,
published_before)``. Semantics are implemented once, by the RAG catalog.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import anyio.to_thread
from fastapi import APIRouter, Depends, Path, Query

from backend.crud.application.use_cases.get_article import GetArticle
from backend.crud.application.use_cases.get_article_summary import GetArticleSummary
from backend.crud.application.use_cases.list_articles import ListArticles
from backend.crud.application.use_cases.list_facets import ListFacets
from backend.crud.domain.catalog import ArticleListParams, CatalogFilterParams
from backend.shared.contracts.catalog import (
    ApiErrorV1,
    ArticleDetailV1,
    ArticleFacetsV1,
    ArticleListV1,
    ArticleSummaryV1,
)

MAX_IDS = 20
IdList = Annotated[
    list[Annotated[str, Query(pattern=r"^[a-z0-9_-]{1,80}$")]] | None,
    Query(max_length=MAX_IDS),
]
UuidList = Annotated[list[UUID] | None, Query(max_length=MAX_IDS)]
ERRORS: dict[int | str, dict[str, object]] = {
    status: {"model": ApiErrorV1} for status in (400, 404, 409, 410, 502, 503)
}


def filter_params(  # noqa: PLR0913, PLR0917 - FastAPI query parameters
    q: Annotated[str | None, Query(max_length=200)] = None,
    topic_ids: IdList = None,
    location_ids: IdList = None,
    source_ids: UuidList = None,
    published_after: Annotated[
        datetime | None, Query(description="ISO-8601 with offset, inclusive")
    ] = None,
    published_before: Annotated[
        datetime | None, Query(description="ISO-8601 with offset, exclusive")
    ] = None,
    include_descendants: bool = True,  # noqa: FBT001, FBT002
) -> CatalogFilterParams:
    """Bounded public filter parameters."""
    return CatalogFilterParams(
        q=q,
        topic_ids=tuple(topic_ids or ()),
        location_ids=tuple(location_ids or ()),
        source_ids=tuple(source_ids or ()),
        published_after=published_after,
        published_before=published_before,
        include_descendants=include_descendants,
    )


Filters = Annotated[CatalogFilterParams, Depends(filter_params)]


async def off_loop[T](call: Callable[[], T]) -> T:
    """Blocking adapter call in a worker thread."""
    return await anyio.to_thread.run_sync(call)


@dataclass(frozen=True, slots=True)
class ArticleUseCases:
    """Composed article use cases."""

    list_articles: ListArticles
    get_article: GetArticle
    get_summary: GetArticleSummary
    list_facets: ListFacets


def build_articles_router(cases: ArticleUseCases) -> APIRouter:
    """Article routes."""
    router = APIRouter(prefix="/api/v1", responses=ERRORS)

    @router.get("/articles")
    async def articles(
        filters: Filters,
        sort: Annotated[
            Literal["latest", "oldest", "relevance"] | None, Query()
        ] = None,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ) -> ArticleListV1:
        params = ArticleListParams(filters, sort, cursor, limit)
        return await off_loop(lambda: cases.list_articles.execute(params))

    @router.get("/articles/{article_id}")
    async def article(
        article_id: Annotated[UUID, Path()],
        article_version_id: Annotated[
            str | None, Query(pattern=r"^sha256:[0-9a-f]{64}$")
        ] = None,
    ) -> ArticleDetailV1:
        return await off_loop(
            lambda: cases.get_article.execute(article_id, article_version_id)
        )

    @router.get("/articles/{article_id}/summary")
    async def summary(article_id: Annotated[UUID, Path()]) -> ArticleSummaryV1:
        return await off_loop(lambda: cases.get_summary.execute(article_id))

    @router.get("/article-facets")
    async def facets(filters: Filters) -> ArticleFacetsV1:
        return await off_loop(lambda: cases.list_facets.execute(filters))

    return router
