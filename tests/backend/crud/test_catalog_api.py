"""crud-api catalog routes over a fake ``ArticleCatalogPort`` (no RAG)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import httpx2
import pytest

from backend.crud.application.ports.article_catalog import (
    CatalogContractError,
    CatalogUnavailableError,
    CatalogUpstreamError,
)
from backend.crud.composition import compose
from backend.crud.domain.catalog import ArticleListParams, CatalogFilterParams
from backend.crud.infra.rag_catalog_http import CircuitBreaker, RagCatalogHttp
from backend.crud.main import create_app
from backend.shared.contracts.catalog import (
    ArticleDetailV1,
    ArticleFacetsV1,
    ArticleListV1,
    ArticleSummaryV1,
    LocationListV1,
    SourceListV1,
    TopicListV1,
)

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 10, 29, tzinfo=UTC)


@dataclass(slots=True)
class FakeCatalog:
    """Records parameters; raises a scripted error when set."""

    error: Exception | None = None
    seen: list[ArticleListParams] = field(default_factory=list[ArticleListParams])

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    def list_articles(self, params: ArticleListParams) -> ArticleListV1:
        self._raise()
        self.seen.append(params)
        return ArticleListV1(
            schema_version="article_list.v1",
            items=(),
            next_cursor=None,
            has_more=False,
            as_of=NOW,
            search_mode="none",
        )

    def get_article(
        self, article_id: UUID, article_version_id: str | None
    ) -> ArticleDetailV1:
        del article_id, article_version_id
        self._raise()
        raise AssertionError

    def get_summary(self, article_id: UUID) -> ArticleSummaryV1:
        del article_id
        self._raise()
        raise AssertionError

    def facets(self, filters: CatalogFilterParams) -> ArticleFacetsV1:
        del filters
        self._raise()
        raise AssertionError

    def topics(self) -> TopicListV1:
        self._raise()
        return TopicListV1(
            schema_version="topic_list.v1", taxonomy_version="t", items=()
        )

    def locations(
        self, parent_id: str | None, q: str | None, limit: int
    ) -> LocationListV1:
        del parent_id, q, limit
        self._raise()
        raise AssertionError

    def sources(self) -> SourceListV1:
        self._raise()
        raise AssertionError


def _client(catalog: FakeCatalog) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_app(compose(catalog))),
        base_url="http://testserver",
    )


async def test_repeated_parameters_are_forwarded_as_received() -> None:
    catalog = FakeCatalog()
    source = uuid4()
    async with _client(catalog) as client:
        response = await client.get(
            "/api/v1/articles",
            params=[
                ("topic_ids", "economy"),
                ("topic_ids", "health"),
                ("location_ids", "vn-ha-noi"),
                ("source_ids", str(source)),
                ("published_after", "2026-10-28T17:00:00+00:00"),
                ("limit", "5"),
            ],
        )
    assert response.status_code == 200
    params = catalog.seen[0]
    assert params.filters.topic_ids == ("economy", "health")
    assert params.filters.source_ids == (source,)
    assert ("topic_ids", "health") in params.items()
    assert params.limit == 5


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (CatalogUpstreamError(410, "article_gone", None), 410, "article_gone"),
        (CatalogUpstreamError(400, "cursor_mismatch", None), 400, "cursor_mismatch"),
        (CatalogUnavailableError("catalog_unreachable"), 503, "catalog_unavailable"),
        (CatalogContractError(), 502, "catalog_contract_mismatch"),
    ],
)
async def test_errors_keep_the_api_envelope_and_never_return_empty(
    error: Exception, status: int, code: str
) -> None:
    async with _client(FakeCatalog(error=error)) as client:
        response = await client.get("/api/v1/articles")
    assert response.status_code == status
    body = cast("dict[str, dict[str, str]]", response.json())
    assert body["schema_version"] == "api_error.v1"
    assert body["error"]["code"] == code
    assert "items" not in body
    if status == 503:
        assert response.headers["Retry-After"] == "5"


async def test_bad_parameters_are_rejected_before_the_catalog() -> None:
    catalog = FakeCatalog()
    async with _client(catalog) as client:
        too_many = await client.get(
            "/api/v1/articles", params=[("topic_ids", f"t{i}") for i in range(21)]
        )
        bad_limit = await client.get("/api/v1/articles", params={"limit": 500})
    assert too_many.status_code == bad_limit.status_code == 400
    assert catalog.seen == []


def test_adapter_without_service_token_is_unavailable_not_empty() -> None:
    adapter = RagCatalogHttp("http://127.0.0.1:9", None)
    with pytest.raises(CatalogUnavailableError) as caught:
        _ = adapter.topics()
    assert caught.value.reason == "service_token_not_configured"


def test_circuit_opens_after_consecutive_failures_and_half_opens() -> None:
    now = [0.0]
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=10, clock=lambda: now[0])
    adapter = RagCatalogHttp("http://127.0.0.1:9", "token", 0.2, breaker)
    for _ in range(2):
        with pytest.raises(CatalogUnavailableError):
            _ = adapter.topics()
    with pytest.raises(CatalogUnavailableError) as caught:
        _ = adapter.topics()
    assert caught.value.reason == "circuit_open"
    now[0] = 11.0
    assert breaker.allow()
