"""Regressions for the T5 independent re-review (R5, R6; 2026-10-18).

R5 every return path (empty / filter_unavailable / zero-hit / evidence) passes
the same final access boundary; a denied caller gets no counts or coverage.
R6 the per-article lexical cut follows the frozen v2 tie-break
(score, document rank, publication time, article_id, chunk_id) exactly.

The store wrapper delegates every other call to the real PostgreSQL store, so
these tests run unchanged against any version of the retrieval store API.
"""

import random
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import TYPE_CHECKING, cast, final
from uuid import UUID, uuid5

import pytest
from retrieval_support import (  # pyright: ignore[reportImplicitRelativeImport]
    CALLER,
    MutableAccess,
    article,
    briefing,
    control,
    service,
)

from eventlens_rag.adapters.postgres import PostgresRagStore
from eventlens_rag.adapters.postgres_retrieval import PostgresRetrievalStore
from eventlens_rag.application.retrieve import RetrievalOptions
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.chunking import DEFAULT_POLICY
from eventlens_rag.domain.retrieval import (
    CHUNK_NAMESPACE,
    RetrievalAccessDeniedError,
)

if TYPE_CHECKING:
    from eventlens_rag.ports.retrieval import RetrievalStore

LEXICAL_NO_RERANK = RetrievalOptions(vector_enabled=False, rerank_enabled=False)


@final
class PausingStore:
    """Real store; after ``method``'s real SQL read finishes, wait on an Event."""

    def __init__(self, inner: PostgresRetrievalStore, method: str) -> None:
        self.inner: PostgresRetrievalStore = inner
        self.method: str = method
        self.read_done: Event = Event()
        self.release: Event = Event()

    def __getattr__(self, name: str) -> object:
        target = cast("object", getattr(self.inner, name))
        if name != self.method or not callable(target):
            return target

        def paused(*args: object, **kwargs: object) -> object:
            result = target(*args, **kwargs)  # real read completed
            self.read_done.set()
            assert self.release.wait(timeout=30)
            return result

        return paused


def revoke_during(store: PausingStore, **query: object) -> MutableAccess:
    access = MutableAccess()
    retrieve = service(
        cast("RetrievalStore", cast("object", store)), embeddings=None, access=access
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            retrieve.execute,
            briefing("harbor", **query),
            CALLER,
            control(),
            RetrievalOptions(vector_enabled=False),
        )
        assert store.read_done.wait(timeout=30)
        access.revoked = True  # revocation completes while the read is in flight
        store.release.set()
        with pytest.raises(RetrievalAccessDeniedError) as denied:
            _ = future.result(timeout=30)
    assert "scoped" not in str(denied.value)  # no counts/coverage in the error
    return access


def test_r5_revoked_during_metadata_read_denies_filter_unavailable_return(
    retrieval_url: str,
) -> None:
    PostgresRagStore(retrieval_url).persist_article(
        article("Harbor crane inspection closes the gate.")
    )  # no READY metadata -> this query would be filter_unavailable
    store = PausingStore(PostgresRetrievalStore(retrieval_url), "metadata_eligibility")

    access = revoke_during(store, topic_filters=("transportation",))

    assert access.revoked


def test_r5_revoked_during_zero_hit_lexical_read_denies_empty_return(
    retrieval_url: str,
) -> None:
    PostgresRagStore(retrieval_url).persist_article(
        article("Completely unrelated bakery opening news.", title="Bakery news")
    )  # unfiltered query "harbor" -> zero hits (title must not match either)
    method = next(
        name
        for name in ("lexical_chunks", "lexical_documents")
        if hasattr(PostgresRetrievalStore, name)
    )
    store = PausingStore(PostgresRetrievalStore(retrieval_url), method)

    access = revoke_during(store)

    assert access.revoked


# R6 ---------------------------------------------------------------------------


def tied_article(article_id: UUID) -> ArticleDocument:
    """Seven chunks; only the title holds the query word, bodies are distinct."""
    words = " ".join(f"w{article_id.hex[:6]}n{i}" for i in range(1100))
    body = words[:7200]
    assert len(body) == 7200  # 1200 + 6 * 1000 -> exactly seven T3 chunks
    return article(body, article_id=article_id, title="Harborword bulletin")


def frozen_tie_order(document: ArticleDocument, kept: int) -> list[int]:
    """Policy order for equal score/doc-rank/time within one article: chunk_id ASC.

    Computed from the chunk identity definition (UUIDv5 over article, hash,
    chunker version and index), independently of any SQL.
    """

    def chunk_id(index: int) -> str:
        name = (
            f"{document.article_id}|{document.content_hash}|"
            f"{DEFAULT_POLICY.version}|{index}"
        )
        return str(uuid5(CHUNK_NAMESPACE, name))

    return sorted(range(7), key=chunk_id)[:kept]


def test_r6_per_article_cut_uses_frozen_chunk_id_tie_break(
    retrieval_url: str,
) -> None:
    rng = random.Random(6)  # noqa: S311 - deterministic shuffled insert order
    documents = [tied_article(UUID(int=rng.getrandbits(128))) for _ in range(4)]
    rng.shuffle(documents)
    store = PostgresRagStore(retrieval_url)
    for document in documents:
        store.persist_article(document)

    result = service(PostgresRetrievalStore(retrieval_url), embeddings=None).execute(
        briefing("harborword"), CALLER, control(), LEXICAL_NO_RERANK
    )

    by_id = {a.article_id: a for a in result.articles}
    assert set(by_id) == {d.article_id for d in documents}
    for document in documents:
        kept = [span.chunk_index for span in by_id[document.article_id].spans]
        assert kept == frozen_tie_order(document, 3)
