from backend.rag.domain.access import CallerScope
from backend.rag.domain.answer import BriefingAnswer
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding_policy import EmbeddingPolicy
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.temporal import TemporalClaim


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
    assert all(model.__module__.startswith("backend.rag.domain.") for model in models)
