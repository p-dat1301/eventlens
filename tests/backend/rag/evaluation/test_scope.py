"""Per-question scope: the wrapper narrows every read and the check catches leaks."""

from uuid import UUID

import pytest

from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.domain.retrieval import ChunkRef, RetrievalStorageError
from backend.rag.evaluation.corpus import load_corpus
from backend.rag.evaluation.rag_support import (
    ask,
    briefing_query,
    fake_model,
    hybrid_store,
    runtime,
)
from backend.rag.evaluation.scope import ScopedRetrievalStore, scope_violations

HARBOR = UUID("00000000-0000-4000-8000-000000000001")
RESTRICTED = UUID("00000000-0000-4000-8000-000000000014")
Q7 = "What restricted budget detail is available?"


def test_unscoped_runtime_leaks_the_restricted_article_and_the_check_sees_it() -> None:
    # Given: the full corpus with no scope (the reviewed defect).
    model = fake_model()
    composed, _ = runtime(hybrid_store(load_corpus()), model)

    # When
    asked = ask(composed, briefing_query(Q7))
    violations = scope_violations(asked.outcome, model.requests, frozenset({HARBOR}))

    # Then
    assert str(RESTRICTED) in str(violations["retrieved"])
    assert str(RESTRICTED) in str(violations["sent_to_model"])


def test_scoped_runtime_answers_q7_without_any_out_of_scope_article() -> None:
    # Given
    model = fake_model()
    scoped = ScopedRetrievalStore(hybrid_store(load_corpus()), frozenset({HARBOR}))
    composed, _ = runtime(scoped, model)

    # When
    asked = ask(composed, briefing_query(Q7))

    # Then
    assert scope_violations(asked.outcome, model.requests, frozenset({HARBOR})) == {
        "retrieved": [],
        "sent_to_model": [],
        "cited": [],
    }


def test_scoped_store_narrows_reads_and_refuses_foreign_chunks() -> None:
    # Given
    store = hybrid_store(load_corpus())
    scoped = ScopedRetrievalStore(store, frozenset({HARBOR}))
    everything = RetrievalScope(
        source_domains=(),
        published_after=None,
        published_before=None,
        allowed_article_ids=None,
    )
    restricted = store.documents[RESTRICTED]

    # When
    current = scoped.current_articles((HARBOR, RESTRICTED))
    eligibility = scoped.metadata_eligibility(everything, (), (), 100)

    # Then
    assert [s.document.article_id for s in current] == [HARBOR]
    assert eligibility.scoped_articles == 1
    with pytest.raises(RetrievalStorageError):
        _ = scoped.chunk_texts(
            (
                ChunkRef(
                    article_id=RESTRICTED,
                    content_hash=restricted.content_hash,
                    chunker_version="fixture",
                    chunk_index=0,
                    start_offset=0,
                    end_offset=5,
                ),
            )
        )
