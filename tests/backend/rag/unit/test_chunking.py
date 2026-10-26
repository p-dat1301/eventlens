from datetime import UTC, datetime
from uuid import UUID

import pytest

from backend.rag.domain.article_document import (
    ArticleDocument,
    normalized_content_hash,
)
from backend.rag.domain.chunking import ChunkingPolicy, chunk_document


def document(content: str) -> ArticleDocument:
    article_id = UUID("00000000-0000-4000-8000-000000000001")
    digest = normalized_content_hash(content)
    return ArticleDocument.model_validate(
        {
            "schema_version": "article_document.v1",
            "article_id": article_id,
            "source_id": UUID("00000000-0000-4000-8000-000000000002"),
            "title": "Chunk test",
            "raw_content": content,
            "normalization_version": "nfc.v1",
            "normalized_content": content,
            "canonical_url": "https://example.test/chunk",
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


def test_chunk_document_uses_codepoint_size_and_overlap() -> None:
    # Given
    source = document("ab😀中def")

    # When
    chunks = chunk_document(source, ChunkingPolicy(size=4, overlap=2))

    # Then
    assert [
        (chunk.start_offset, chunk.end_offset, chunk.content) for chunk in chunks
    ] == [
        (0, 4, "ab😀中"),
        (2, 6, "😀中de"),
        (4, 7, "def"),
    ]


def test_chunking_policy_rejects_non_progressing_overlap() -> None:
    # Given / When / Then
    with pytest.raises(ValueError, match="smaller"):
        _ = ChunkingPolicy(size=1200, overlap=1200)
