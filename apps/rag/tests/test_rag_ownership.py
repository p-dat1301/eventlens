from eventlens_rag.domain.access import CallerScope
from eventlens_rag.domain.answer import BriefingAnswer
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.embedding_policy import EmbeddingPolicy
from eventlens_rag.domain.query import BriefingQuery
from eventlens_rag.domain.temporal import TemporalClaim


def test_rag_models_owned_by_rag() -> None:
    # Given / When
    models = (
        CallerScope,
        BriefingAnswer,
        ArticleDocument,
        EmbeddingPolicy,
        BriefingQuery,
        TemporalClaim,
    )

    # Then
    assert all(model.__module__.startswith("eventlens_rag.domain.") for model in models)
