"""Deterministic event extractor for local and test workflows."""

from typing import Final
from uuid import NAMESPACE_URL, uuid5

from eventlens_contracts import Event
from eventlens_worker.bundle import ArticleBundle
from eventlens_worker.models import EventProposal

EXTRACTOR_VERSION: Final = "fake.v1"
CONFIDENCE: Final = 0.90


class FakeEventExtractor:
    """Build one stable scheduled event from each validated article bundle."""

    def extract(self, bundle: ArticleBundle) -> EventProposal:
        """Return deterministic proposal derived only from bundle data."""
        article = bundle.article
        lifecycle_at = article.published_at or article.processing.extracted_at
        event_id = uuid5(NAMESPACE_URL, f"eventlens:event:{article.article_id}")

        return EventProposal(
            proposal_id=uuid5(
                NAMESPACE_URL,
                f"eventlens:proposal:{article.article_id}:{EXTRACTOR_VERSION}",
            ),
            article_id=article.article_id,
            extractor_version=EXTRACTOR_VERSION,
            event=Event(
                id=event_id,
                event_type="SCHEDULED_EVENT",
                canonical_title=article.title,
                status="detected",
                severity="info",
                time_start=article.published_at,
                time_end=None,
                time_precision="exact" if article.published_at else "unknown",
                first_seen_at=lifecycle_at,
                last_seen_at=lifecycle_at,
                last_changed_at=lifecycle_at,
                source_count=1,
                article_count=1,
                confidence=CONFIDENCE,
                attributes=None,
                schema_version="event.v1",
                created_at=lifecycle_at,
                updated_at=lifecycle_at,
                entities=(),
            ),
        )
