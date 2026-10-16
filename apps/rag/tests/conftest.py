import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter

from eventlens_contracts import ArticleEnrichment
from eventlens_rag.adapters.postgres import PostgresRagStore
from eventlens_rag.domain.article_document import (
    ArticleDocument,
    normalized_content_hash,
)

MIGRATIONS = Path(__file__).parents[1] / "src/eventlens_rag/migrations"
ENRICHMENT_FIXTURE = Path(__file__).parents[3] / "fixtures/rag/enrichments/01.json"

type ArticleFactory = Callable[[str], ArticleDocument]


@pytest.fixture
def database_url() -> str:
    value = os.environ.get("EVENTLENS_RAG_DATABASE_URL")
    if value is None:
        pytest.skip("EVENTLENS_RAG_DATABASE_URL required for real PostgreSQL test")
    PostgresRagStore(value).migrate(MIGRATIONS)
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
