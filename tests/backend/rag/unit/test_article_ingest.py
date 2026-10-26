from datetime import UTC, datetime, timedelta
from pathlib import Path
from unicodedata import normalize

import pytest
from pydantic import TypeAdapter, ValidationError

from backend.rag.application.use_cases.ingest import IngestArticle
from backend.rag.application.use_cases.persist import AccessDeniedError
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_document import normalized_content_hash
from backend.rag.domain.article_ingest import IngestClock, document_from_article
from backend.rag.domain.enrichment_lifecycle import EnrichmentIdentity
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.repositories.memory_enrichment import (
    InMemoryEnrichmentRepository,
)
from backend.shared.contracts.article import Article

BUNDLE = Path(__file__).parents[4] / "fixtures/article_bundle.v1.json"
CALLER = CallerScope(
    schema_version="caller_scope.v1",
    principal_id="fixture-analyst",
    scope_id="fixture-internal",
    policy_id="fixture-policy",
    policy_version="v1",
)
IDENTITY = EnrichmentIdentity("article-enrichment.v1", "m1", "p1")
INGESTED = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
PROCESSED = INGESTED + timedelta(milliseconds=5)


def upstream_article(content: str | None = None) -> Article:
    source = TypeAdapter(dict[str, object]).validate_json(BUNDLE.read_bytes())
    article = Article.model_validate_json(
        TypeAdapter(object).dump_json(source["article"])
    )
    if content is None:
        return article
    return article.model_copy(update={"content": content})


def test_conversion_hashes_actual_nfc_content_not_upstream_hash() -> None:
    # Given: decomposed Vietnamese text (NFD) under an upstream hash of other bytes.
    decomposed = normalize("NFD", "Thành phố mở công viên mới.")
    article = upstream_article(decomposed)
    # When
    result = document_from_article(article, IngestClock(INGESTED, PROCESSED))
    # Then
    nfc = normalize("NFC", decomposed)
    assert result.raw_content == decomposed
    assert result.normalized_content == nfc
    assert result.content_hash == normalized_content_hash(nfc)
    assert result.content_hash != article.content_hash
    assert result.content_fingerprint == result.content_hash


def test_conversion_maps_three_distinct_timestamps_and_no_invented_provenance() -> None:
    article = upstream_article()
    before = article.model_dump()
    result = document_from_article(article, IngestClock(INGESTED, PROCESSED))
    assert result.source_published_at == article.published_at
    assert result.ingested_at == INGESTED
    assert result.processed_at == PROCESSED
    assert result.ingested_at not in (article.raw.fetched_at, article.published_at)
    assert result.processed_at != article.processing.extracted_at
    assert result.evidence_spans == ()
    assert result.source_name == article.source.name
    assert result.source_domain == article.source.domain
    assert article.model_dump() == before


def test_missing_publication_time_stays_unknown() -> None:
    article = upstream_article().model_copy(update={"published_at": None})
    result = document_from_article(article, IngestClock(INGESTED, PROCESSED))
    assert result.source_published_at is None


def test_processing_before_ingestion_is_rejected_by_document_contract() -> None:
    with pytest.raises(ValidationError, match="invalid_processing_time"):
        _ = document_from_article(upstream_article(), IngestClock(PROCESSED, INGESTED))


def test_ingest_service_persists_and_enqueues_once_for_duplicate_delivery() -> None:
    clock_values = iter([INGESTED, PROCESSED, INGESTED, PROCESSED])
    repository = InMemoryEnrichmentRepository(clock=lambda: INGESTED)
    service = IngestArticle(
        repository, FixtureAccessPolicy(), IDENTITY, lambda: next(clock_values)
    )
    first = service.execute(upstream_article(), CALLER)
    second = service.execute(upstream_article(), CALLER)
    assert first.job_created
    assert not second.job_created
    assert first.job_id == second.job_id
    assert len(repository.jobs) == 1


def test_denied_caller_starts_no_persistence() -> None:
    repository = InMemoryEnrichmentRepository(clock=lambda: INGESTED)
    service = IngestArticle(
        repository, FixtureAccessPolicy(revoked=True), IDENTITY, lambda: INGESTED
    )
    with pytest.raises(AccessDeniedError):
        _ = service.execute(upstream_article(), CALLER)
    assert repository.calls == []
