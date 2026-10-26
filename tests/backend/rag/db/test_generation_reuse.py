import re
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from typer.testing import CliRunner

from backend.rag.application.use_cases.index import EnsureGeneration
from backend.rag.cli_main import UNAVAILABLE_EXIT, app
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity, EmbeddingVector
from backend.rag.domain.staging import StageRequest
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings
from backend.rag.infra.postgres.connection import StorageUnavailableError, connect
from backend.rag.infra.postgres.staging import PostgresStaging
from backend.rag.infra.postgres.store import PostgresRagStore

type Factory = Callable[[str], ArticleDocument]
CLOSED = "postgresql://user:secret-password@127.0.0.1:1/missing"


@dataclass(slots=True)
class CountingEmbeddings:
    calls: int = 0
    inner: FakeEmbeddings = field(default_factory=FakeEmbeddings)

    def embed(self, text: str) -> EmbeddingVector:
        self.calls += 1
        return self.inner.embed(text)


@dataclass(slots=True)
class MemoryStaging:
    staged: dict[tuple[str, EmbeddingIdentity], int] = field(default_factory=dict)

    def find_complete(
        self, article: ArticleDocument, identity: EmbeddingIdentity
    ) -> int | None:
        return self.staged.get((article.content_hash, identity))

    def stage(self, request: StageRequest) -> int:
        key = (request.article.content_hash, request.identity)
        return self.staged.setdefault(key, len(self.staged) + 1)


def test_metadata_only_revision_reuses_vectors_without_embedding_calls(
    make_article: Factory,
) -> None:
    embeddings = CountingEmbeddings()
    staging = MemoryStaging()
    ensure = EnsureGeneration(staging, embeddings, EmbeddingIdentity())
    article = make_article("Reusable vectors")
    first = ensure.execute(article)
    calls = embeddings.calls
    second = ensure.execute(article)
    assert (calls, embeddings.calls) == (1, 1)
    assert second.generation == first.generation
    assert (first.reused, second.reused) == (False, True)


def test_content_or_embedding_identity_change_creates_new_generation(
    make_article: Factory,
) -> None:
    embeddings = CountingEmbeddings()
    staging = MemoryStaging()
    article = make_article("Original text")
    base = EnsureGeneration(staging, embeddings, EmbeddingIdentity()).execute(article)
    changed = EnsureGeneration(staging, embeddings, EmbeddingIdentity()).execute(
        make_article("Corrected text")
    )
    rebuilt = EnsureGeneration(
        staging, embeddings, EmbeddingIdentity(corpus_generation="corpus.v2")
    ).execute(article)
    assert len({base.generation, changed.generation, rebuilt.generation}) == 3
    assert embeddings.calls == 3


def test_unreachable_database_raises_typed_error_without_credentials() -> None:
    with pytest.raises(StorageUnavailableError) as raised:
        _ = connect(CLOSED)
    assert raised.value.code == "rag_storage_unavailable"
    assert "secret-password" not in str(raised.value)


def test_cli_reports_typed_unavailable_exit() -> None:
    result = CliRunner().invoke(app, ["migrate", "--database-url", CLOSED])
    assert result.exit_code == UNAVAILABLE_EXIT
    assert "rag_storage_unavailable" in result.output
    assert "secret-password" not in result.output


def test_real_database_reuse_skips_embedding_calls(
    database_url: str, make_article: Factory
) -> None:
    article = make_article("Real database reuse")
    PostgresRagStore(database_url).persist_article(article)
    embeddings = CountingEmbeddings()
    staging = PostgresStaging(database_url)
    ensure = EnsureGeneration(staging, embeddings, EmbeddingIdentity())
    first = ensure.execute(article)
    second = ensure.execute(article)
    assert embeddings.calls == 1
    assert second == type(second)(first.generation, reused=True)


def test_cli_rebuild_publishes_fixture_corpus_then_reuses_it(database_url: str) -> None:
    runner = CliRunner()
    url = ["--database-url", database_url]
    loaded = runner.invoke(app, ["load-fixtures", *url])
    first = runner.invoke(app, ["rebuild", *url])
    second = runner.invoke(app, ["rebuild", *url])
    assert loaded.exit_code == first.exit_code == second.exit_code == 0
    assert "persisted 30 fixture articles" in loaded.output
    reused = re.search(
        r"published (\d+) current articles \((\d+) reused", second.output
    )
    assert reused is not None
    assert reused.group(1) == reused.group(2)
    with connect(database_url) as connection:
        row = connection.execute(
            """SELECT count(DISTINCT article_id) FROM rag_current_vectors
            WHERE article_id::text LIKE '00000000-0000-4000-8000-0000000000%'"""
        ).fetchone()
    assert row == (30,)
