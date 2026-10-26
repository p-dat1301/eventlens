from datetime import UTC, datetime
from uuid import UUID

from backend.rag.domain.records import ArticleRevision, EnrichmentJobKey
from backend.rag.domain.storage import InMemoryRagStore


def revision(content_hash: str = "sha256:" + "a" * 64) -> ArticleRevision:
    return ArticleRevision(
        article_id=UUID("00000000-0000-4000-8000-000000000001"),
        content_hash=content_hash,
        normalized_content="Lexical searchable article",
        indexed_at=datetime(2026, 9, 29, tzinfo=UTC),
    )


def test_store_preserves_immutable_revisions_and_tombstones_current_article() -> None:
    # Given
    store = InMemoryRagStore()
    original = revision()
    corrected = revision("sha256:" + "b" * 64)

    # When
    store.persist_article(original)
    store.persist_article(corrected)
    store.tombstone(original.article_id)

    # Then
    assert store.revisions(original.article_id) == (original, corrected)
    assert store.current_article(original.article_id) is None


def test_job_claim_uses_five_field_key_and_generation_fence() -> None:
    # Given
    store = InMemoryRagStore()
    store.persist_article(revision())
    key = EnrichmentJobKey(
        article_id=revision().article_id,
        content_hash=revision().content_hash,
        enrichment_version="enrichment.v1",
        model_version="model.v1",
        prompt_version="prompt.v1",
    )

    # When
    initial = store.enqueue_job(key)
    first = store.claim_job(initial.job_id)
    reclaimed = store.claim_job(initial.job_id)

    # Then
    assert store.enqueue_job(key).job_id == initial.job_id
    assert store.complete_job(initial.job_id, first.generation) is False
    assert store.complete_job(initial.job_id, reclaimed.generation) is True
