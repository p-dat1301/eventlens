"""crud-api composition root: the only place CRUD adapters are built.

| Variable | Default |
| --- | --- |
| ``EVENTLENS_CRUD_RAG_CATALOG_URL`` | ``http://127.0.0.1:8001`` (rag-api, internal) |
| ``EVENTLENS_CRUD_RAG_SERVICE_TOKEN`` | unset: catalog routes answer 503 |
| ``EVENTLENS_CRUD_RAG_TIMEOUT_SECONDS`` | ``3`` |
"""

from collections.abc import Mapping
from dataclasses import dataclass

from backend.crud.application.ports.article_catalog import ArticleCatalogPort
from backend.crud.application.use_cases.get_article import GetArticle
from backend.crud.application.use_cases.get_article_summary import GetArticleSummary
from backend.crud.application.use_cases.list_articles import ListArticles
from backend.crud.application.use_cases.list_facets import ListFacets
from backend.crud.application.use_cases.taxonomy import BrowseTaxonomy
from backend.crud.infra.rag_catalog_http import RagCatalogHttp
from backend.crud.presentation.articles import ArticleUseCases

DEFAULT_CATALOG_URL = "http://127.0.0.1:8001"


@dataclass(frozen=True, slots=True)
class CrudRuntime:
    """Composed CRUD use cases."""

    articles: ArticleUseCases
    taxonomy: BrowseTaxonomy


def compose(catalog: ArticleCatalogPort) -> CrudRuntime:
    """Wire use cases over one catalog port."""
    return CrudRuntime(
        articles=ArticleUseCases(
            list_articles=ListArticles(catalog),
            get_article=GetArticle(catalog),
            get_summary=GetArticleSummary(catalog),
            list_facets=ListFacets(catalog),
        ),
        taxonomy=BrowseTaxonomy(catalog),
    )


def runtime_from_environment(environ: Mapping[str, str]) -> CrudRuntime:
    """HTTP catalog adapter from the environment."""
    timeout = float(environ.get("EVENTLENS_CRUD_RAG_TIMEOUT_SECONDS", "3") or "3")
    return compose(
        RagCatalogHttp(
            environ.get("EVENTLENS_CRUD_RAG_CATALOG_URL", "").strip()
            or DEFAULT_CATALOG_URL,
            environ.get("EVENTLENS_CRUD_RAG_SERVICE_TOKEN", "").strip() or None,
            timeout_seconds=timeout,
        )
    )
