"""Scripted enrichment drafts and article revisions for lifecycle cases."""

from backend.rag.domain.article_document import ArticleDocument, normalized_content_hash
from backend.rag.domain.enrichment_draft import (
    DraftEntity,
    DraftEvidence,
    DraftFieldConfidence,
    EnrichmentDraft,
)

ENTITY = "Harbor Authority"


def draft_for(
    doc: ArticleDocument,
    *,
    topics: tuple[str, ...] = ("transportation",),
    entity: str = ENTITY,
    refs: tuple[int, ...] = (0,),
    suggested: tuple[str, ...] = (),
) -> EnrichmentDraft:
    """Grounded draft citing the exact entity span (overridable fields)."""
    start = doc.normalized_content.index(ENTITY)
    return EnrichmentDraft(
        topics=topics,
        content_type="news_report",
        event_relevance="actionable_event",
        entities=(
            DraftEntity(
                entity_type="ORG",
                canonical_name=entity,
                surface_forms=(entity,),
                evidence_refs=refs,
                confidence=0.9,
            ),
        ),
        locations=(),
        suggested_tags=suggested,
        evidence=(
            DraftEvidence(
                start_offset=start, end_offset=start + len(ENTITY), excerpt=ENTITY
            ),
        ),
        field_confidence=(DraftFieldConfidence(field="topics", confidence=0.8),),
    )


def revised(doc: ArticleDocument) -> ArticleDocument:
    """New revision of ``doc`` with appended content and a new hash."""
    content = doc.normalized_content + " Update: gate inspection extended."
    digest = normalized_content_hash(content)
    return doc.model_copy(
        update={
            "raw_content": content,
            "normalized_content": content,
            "content_hash": digest,
            "content_fingerprint": digest,
            "evidence_spans": (),
        }
    )
