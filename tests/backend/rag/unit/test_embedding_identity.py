import pytest

from backend.rag.domain.embedding import EmbeddingIdentity


def test_embedding_identity_rejects_schema_dimension_change() -> None:
    with pytest.raises(ValueError, match="dimension"):
        _ = EmbeddingIdentity(dimension=3072)


def test_embedding_identity_changes_with_corpus_generation() -> None:
    assert EmbeddingIdentity(corpus_generation="first") != EmbeddingIdentity(
        corpus_generation="second"
    )
