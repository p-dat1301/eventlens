"""Persist-first article ingest with durable enrichment enqueue."""

from dataclasses import dataclass

from eventlens_contracts.article import Article
from eventlens_rag.application.persist import AccessDeniedError
from eventlens_rag.domain.access import CallerScope
from eventlens_rag.domain.article_ingest import IngestClock, document_from_article
from eventlens_rag.domain.enrichment_lifecycle import EnrichmentIdentity, IngestReceipt
from eventlens_rag.ports.access import AccessPolicy
from eventlens_rag.ports.enrichment import Clock, EnrichmentRepository


@dataclass(frozen=True, slots=True)
class IngestArticle:
    """Convert, persist and enqueue; never calls a model.

    Persist and enqueue commit in one repository transaction, so a crash can
    lose neither half alone. Articles persisted by other paths (for example
    ``eventlens-rag load-fixtures``) are picked up by ``enqueue_missing``.
    """

    repository: EnrichmentRepository
    access: AccessPolicy
    identity: EnrichmentIdentity
    clock: Clock

    def execute(self, article: Article, caller: CallerScope) -> IngestReceipt:
        """Authorize, then commit the document and its PENDING job together."""
        if not self.access.allows(caller):
            raise AccessDeniedError(caller.principal_id)
        ingested_at = self.clock()
        document = document_from_article(
            article, IngestClock(ingested_at=ingested_at, processed_at=self.clock())
        )
        key = self.identity.key_for(document.article_id, document.content_hash)
        return self.repository.ingest(document, key)
