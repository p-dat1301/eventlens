from dataclasses import replace
from pathlib import Path

import pytest

from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.chunking import chunk_document
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.staging import InvalidStageError, StageRequest
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings


def document() -> ArticleDocument:
    return ArticleDocument.model_validate_json(
        (Path(__file__).parents[4] / "fixtures/rag_article_document.v1.json").read_text(
            encoding="utf-8"
        )
    )


def test_stage_rejects_missing_vectors() -> None:
    article = document()
    with pytest.raises(InvalidStageError):
        _ = StageRequest(article, EmbeddingIdentity(), chunk_document(article), ())


def test_stage_rejects_mismatched_embedding_identity() -> None:
    article = document()
    vector = FakeEmbeddings().embed(article.normalized_content)
    with pytest.raises(InvalidStageError):
        _ = StageRequest(
            article,
            EmbeddingIdentity(),
            chunk_document(article),
            (replace(vector, version="wrong"),),
        )
