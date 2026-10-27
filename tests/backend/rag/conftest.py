import os
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import TypeAdapter

from backend.rag.domain.article_document import (
    ArticleDocument,
    normalized_content_hash,
)
from backend.rag.infra.postgres.engine import dispose_engines
from backend.shared.contracts import ArticleEnrichment
from schema_support import prepare_schema

ENRICHMENT_FIXTURE = Path(__file__).parents[3] / "fixtures/rag/enrichments/01.json"

type ArticleFactory = Callable[[str], ArticleDocument]


@pytest.fixture(autouse=True)
def _dispose_engines() -> Generator[None]:
    """Close pooled connections so per-test schemas never exhaust the server."""
    yield
    dispose_engines()


@pytest.fixture
def database_url() -> str:
    value = os.environ.get("EVENTLENS_RAG_DATABASE_URL")
    if value is None:
        pytest.skip("EVENTLENS_RAG_DATABASE_URL required for real PostgreSQL test")
    prepare_schema(value)
    return value


def build_article(content: str, article_id: UUID | None = None) -> ArticleDocument:
    digest = normalized_content_hash(content)
    return ArticleDocument.model_validate(
        {
            "schema_version": "article_document.v1",
            "article_id": article_id or uuid4(),
            "source_id": UUID("00000000-0000-4000-8000-000000000012"),
            "title": "Database article",
            "raw_content": content,
            "normalization_version": "nfc.v1",
            "normalized_content": content,
            "canonical_url": "https://example.test/database",
            "source_name": "Example",
            "source_domain": "example.test",
            "source_published_at": None,
            "ingested_at": datetime(2026, 9, 29, tzinfo=UTC),
            "processed_at": datetime(2026, 9, 29, tzinfo=UTC),
            "content_hash": digest,
            "content_fingerprint": digest,
            "visibility": "internal",
            "evidence_spans": (),
        }
    )


@pytest.fixture
def make_article() -> ArticleFactory:
    return build_article


def ready_metadata(article: ArticleDocument, version: str = "v1") -> ArticleEnrichment:
    """Valid READY enrichment re-keyed to one article and a v1 job identity."""
    adapter = TypeAdapter(dict[str, object])
    source = adapter.validate_json(ENRICHMENT_FIXTURE.read_bytes())
    source.update(
        article_id=str(article.article_id),
        content_hash=article.content_hash,
        enrichment_version=version,
        model_version=version,
        prompt_version=version,
        evidence_spans=[],
        entities=[],
        locations=[],
    )
    return ArticleEnrichment.model_validate_json(adapter.dump_json(source))


type MetadataFactory = Callable[[ArticleDocument], ArticleEnrichment]


@pytest.fixture
def make_metadata() -> MetadataFactory:
    return ready_metadata


@pytest.fixture
def retrieval_url(database_url: str) -> Generator[str]:
    """T5: fresh schema per test inside the isolated test container."""
    schema = "t5_" + uuid4().hex
    with psycopg.connect(database_url, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    scoped = make_conninfo(database_url, options=f"-c search_path={schema},public")
    prepare_schema(scoped)
    yield scoped
    with psycopg.connect(database_url, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )
