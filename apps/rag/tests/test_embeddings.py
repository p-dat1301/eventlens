from eventlens_rag.adapters.fake_embeddings import FakeEmbeddings


def test_fake_embeddings_are_deterministic_1536_dimensions() -> None:
    # Given
    embeddings = FakeEmbeddings()

    # When
    first = embeddings.embed("Vietnamese tiếng Việt 中文 😀")
    second = embeddings.embed("Vietnamese tiếng Việt 中文 😀")

    # Then
    assert len(first.values) == 1536
    assert first == second
    assert first.model == "fixture-embedding"
