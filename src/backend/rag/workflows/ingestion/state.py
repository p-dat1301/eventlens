"""Per-invocation channels of the ingestion (enrichment) workflow."""

from typing import TypedDict

from backend.rag.application.use_cases.index import StagedGeneration
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.enrichment_draft import EnrichmentDraft
from backend.rag.domain.enrichment_lifecycle import ClaimedJob, FailureCode, JobOutcome
from backend.shared.contracts import ArticleEnrichment


class EnrichmentChannels(TypedDict, total=False):
    """Per-invocation channels; nothing persists between invocations."""

    claim: ClaimedJob
    article: ArticleDocument
    draft: EnrichmentDraft
    metadata: ArticleEnrichment
    staged: StagedGeneration
    failure: FailureCode
    outcome: JobOutcome
