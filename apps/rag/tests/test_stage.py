from dataclasses import replace
from pathlib import Path

import pytest

from eventlens_rag.adapters.fake_embeddings import FakeEmbeddings
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.chunking import chunk_document
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.domain.staging import InvalidStageError, StageRequest


def document() -> ArticleDocument:
    return ArticleDocument.model_validate_json(
        (Path(__file__).parents[3] / "fixtures/rag_article_document.v1.json").read_text(
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
