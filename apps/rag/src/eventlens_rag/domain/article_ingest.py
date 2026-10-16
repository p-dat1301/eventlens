"""Convert a canonical upstream Article into a searchable ArticleDocument.

Timestamp mapping (never collapsed into one value):

- ``source_published_at`` <- ``Article.published_at``: when the publisher says the
  article was published. ``None`` stays ``None``; it is never back-filled.
- ``ingested_at``: when the RAG ingest boundary received the article (injected
  clock reading taken before conversion).
- ``processed_at``: when RAG normalization finished (second injected clock
  reading, never earlier than ``ingested_at``).

Upstream crawl/parse times (``raw.fetched_at``, ``processing.extracted_at``) and
``updated_at_source`` describe upstream pipeline events and are not mapped.
"""

from dataclasses import dataclass
from datetime import datetime
from unicodedata import normalize

from eventlens_contracts.article import Article
from eventlens_rag.domain.article_document import (
    ArticleDocument,
    normalized_content_hash,
)


@dataclass(frozen=True, slots=True)
class IngestClock:
    """Two RAG-side readings; upstream timestamps are never substituted."""

    ingested_at: datetime
    processed_at: datetime


def document_from_article(article: Article, clock: IngestClock) -> ArticleDocument:
    """Build an immutable document from the actual content, NFC-normalized.

    The upstream ``Article.content_hash`` is not reused: its normalization
    semantics are not guaranteed to match RAG ``nfc.v1``, so the RAG hash is
    recomputed over the NFC text. No evidence spans or provenance are invented.
    """
    normalized = normalize("NFC", article.content)
    digest = normalized_content_hash(normalized)
    return ArticleDocument(
        schema_version="article_document.v1",
        article_id=article.article_id,
        source_id=article.source_id,
        title=article.title,
        raw_content=article.content,
        normalization_version="nfc.v1",
        normalized_content=normalized,
        canonical_url=article.canonical_url,
        source_name=article.source.name,
        source_domain=article.source.domain,
        source_published_at=article.published_at,
        ingested_at=clock.ingested_at,
        processed_at=clock.processed_at,
        content_hash=digest,
        content_fingerprint=digest,
        visibility="internal",
        evidence_spans=(),
    )
