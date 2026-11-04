"""Outbox drain, rebuild, reconcile and projection over in-memory adapters."""

from dataclasses import replace
from datetime import timedelta

import pytest

from backend.rag.application.ports.search_index import SearchIndexError
from backend.rag.application.use_cases.search_projection import (
    ChunkerMismatchError,
    ProjectRevision,
)
from backend.rag.domain.outbox import DrainReport, OutboxRetryPolicy, OutboxStatus
from backend.rag.domain.search_embedding import (
    EmbeddingDimensionMismatchError,
    EmbeddingIdentityMismatchError,
    SearchEmbeddingIdentity,
)
from backend.rag.domain.search_index import (
    GenerationStatus,
    IndexGeneration,
    SearchFilters,
)
from backend.rag.infra.embeddings.fake_bge_m3 import FakeBgeM3Embeddings
from search_support import SearchHarness, article, search_harness

SCOPE = SearchFilters(scope_id="fixture-internal")
PARAGRAPHS = "\n\n".join(
    [
        (
            f"Đoạn {n}: Cảng Hải Phòng tiếp nhận {n * 3} tàu container trong tuần. "
            "Ban quản lý cho biết sản lượng tăng so với cùng kỳ."
        )
        for n in range(12)
    ]
)


def _statuses(h: SearchHarness) -> dict[OutboxStatus, int]:
    return h.outbox.counts()


def _active(h: SearchHarness) -> IndexGeneration:
    generation = h.generations.active()
    assert generation is not None
    return generation


def test_rebuild_projects_postgres_truth_and_activates_atomically() -> None:
    h = search_harness()
    docs = [article(PARAGRAPHS, title=f"Bài {n}") for n in range(3)]
    for doc in docs:
        h.world.persist(doc)

    report = h.rebuild.execute(h.embedder.identity)

    assert report.generation.status is GenerationStatus.ACTIVE
    assert report.revisions == 3
    assert report.indexed == report.chunks > 3
    status = h.index.status(report.generation)
    assert (status.documents, status.revisions) == (report.chunks, 3)
    assert report.generation.embedding_identity == h.embedder.identity.key
    assert report.generation.embedding_dimensions == 1024


def test_rebuild_is_idempotent_and_reuses_cached_embeddings() -> None:
    h = search_harness()
    for n in range(3):
        h.world.persist(article(PARAGRAPHS, title=f"Bài {n}"))
    first = h.rebuild.execute(h.embedder.identity)
    embedded_once = len(h.embedder.texts_embedded)

    second = h.rebuild.execute(h.embedder.identity)

    assert len(h.embedder.texts_embedded) == embedded_once  # zero new embeddings
    assert second.embedded == 0
    assert second.cache_hits == first.chunks
    assert h.index.revision_digests(first.generation) == h.index.revision_digests(
        second.generation
    )
    previous = h.generations.get(first.generation.generation_id)
    assert previous is not None
    assert previous.status is GenerationStatus.RETIRED


def test_interrupted_rebuild_never_activates_and_old_generation_keeps_serving() -> None:
    h = search_harness()
    for n in range(4):
        h.world.persist(article(PARAGRAPHS, title=f"Bài {n}"))
    good = h.rebuild.execute(h.embedder.identity).generation
    h.index.fail_after_writes = h.index.writes + 5

    with pytest.raises(SearchIndexError):
        _ = h.rebuild.execute(h.embedder.identity)

    assert _active(h) == good
    failed = [
        g for g in h.world.generations.values() if g.status is GenerationStatus.FAILED
    ]
    assert len(failed) == 1
    h.index.fail_after_writes = None
    retried = h.rebuild.execute(h.embedder.identity)
    assert retried.generation.status is GenerationStatus.ACTIVE
    assert retried.generation.generation_id > failed[0].generation_id


def test_drain_is_idempotent_and_marks_done_only_after_ack() -> None:
    h = search_harness()
    _ = h.rebuild.execute(h.embedder.identity)
    doc = article(PARAGRAPHS)
    h.world.persist(doc)

    first = h.drain.execute()
    replay_before = h.index.revision_digests(_active(h))
    # A duplicate delivery (same event re-queued) changes nothing.
    h.world.persist(doc)  # same hash: no new event
    h.world.rows[-1].status = OutboxStatus.PENDING
    second = h.drain.execute()

    assert first.done >= 1
    assert second.done == 1
    assert h.index.revision_digests(_active(h)) == replay_before


def test_revision_supersession_leaves_only_the_current_revision() -> None:
    h = search_harness()
    _ = h.rebuild.execute(h.embedder.identity)
    doc = article(PARAGRAPHS)
    h.world.persist(doc)
    _ = h.drain.execute()
    corrected = article(
        PARAGRAPHS + "\n\nĐính chính: số liệu đã cập nhật.", article_id=doc.article_id
    )
    h.world.persist(corrected)

    _ = h.drain.execute()

    digests = h.index.revision_digests(_active(h))
    assert [(d.article_id, d.content_hash) for d in digests] == [
        (doc.article_id, corrected.content_hash)
    ]
    hits = h.index.lexical_search(_active(h), "Đính chính", SCOPE, 10)
    assert {hit.content_hash for hit in hits} == {corrected.content_hash}


def test_tombstone_removes_every_chunk() -> None:
    h = search_harness()
    _ = h.rebuild.execute(h.embedder.identity)
    doc = article(PARAGRAPHS)
    h.world.persist(doc)
    _ = h.drain.execute()

    h.world.tombstone(doc.article_id)
    _ = h.drain.execute()

    assert h.index.revision_digests(_active(h)) == ()
    assert h.index.lexical_search(_active(h), "Hải Phòng", SCOPE, 10) == ()


def test_index_outage_retries_with_backoff_then_dead_letters() -> None:
    policy = OutboxRetryPolicy(max_attempts=3, base_seconds=5, factor=2)
    h = search_harness(policy=policy)
    _ = h.rebuild.execute(h.embedder.identity)
    _ = h.drain.execute()  # activation event
    h.world.persist(article(PARAGRAPHS))
    h.index.unavailable = True

    attempts: list[DrainReport] = []
    for _ in range(3):
        attempts.append(h.drain.execute())
        h.world.now += timedelta(seconds=60)

    assert [a.retried for a in attempts] == [1, 1, 0]
    assert attempts[-1].failed == 1
    counts = _statuses(h)
    assert counts[OutboxStatus.FAILED] == 1
    assert counts[OutboxStatus.DONE] == 1  # only the activation event
    # Source data is untouched by the outage.
    assert len(h.world.documents) == 1
    # Operator requeue after recovery completes the event.
    h.index.unavailable = False
    assert h.outbox.requeue_failed() == 1
    assert h.drain.execute().done == 1


def test_events_wait_when_no_generation_exists() -> None:
    h = search_harness()
    h.world.persist(article(PARAGRAPHS))
    report = h.drain.execute()
    assert report.claimed == 0
    assert _statuses(h)[OutboxStatus.PENDING] == 1


def test_reconcile_detects_and_repairs_missing_stale_and_extra() -> None:
    h = search_harness()
    keep, change, drop = (article(PARAGRAPHS, title=t) for t in ("A", "B", "C"))
    for doc in (keep, change, drop):
        h.world.persist(doc)
    generation = h.rebuild.execute(h.embedder.identity).generation
    # Drift behind the outbox's back (e.g. events lost before this fix).
    h.world.documents[change.article_id] = article(
        PARAGRAPHS + " Bổ sung.", article_id=change.article_id, title="B"
    )
    h.world.tombstoned.add(drop.article_id)
    missing = article(PARAGRAPHS, title="D")
    h.world.documents[missing.article_id] = missing

    report = h.reconcile.execute(deep=True)
    assert set(report.missing) == {missing.article_id}
    assert set(report.stale) == {change.article_id}
    assert set(report.extra) == {drop.article_id}
    assert not report.consistent

    repaired = h.reconcile.execute(repair=True)
    assert repaired.repaired == 3
    assert h.reconcile.execute(deep=True).consistent
    assert generation == _active(h)


def test_deep_reconcile_flags_chunk_count_mismatch() -> None:
    h = search_harness()
    doc = article(PARAGRAPHS)
    h.world.persist(doc)
    generation = h.rebuild.execute(h.embedder.identity).generation
    store = h.index.indices[generation.index_name]
    victim = next(k for k, d in store.items() if d.chunk_index == 1)
    del store[victim]

    report = h.reconcile.execute(deep=True)

    assert report.chunk_mismatch == (doc.article_id,)
    assert h.reconcile.execute(repair=True, deep=True).repaired == 1
    assert h.reconcile.execute(deep=True).consistent


def test_projection_refuses_a_mismatched_embedder() -> None:
    h = search_harness()
    h.world.persist(article(PARAGRAPHS))
    generation = h.rebuild.execute(h.embedder.identity).generation
    other = FakeBgeM3Embeddings(
        identity=SearchEmbeddingIdentity("bge-m3-1024.v1", "BAAI/bge-m3", 1024)
    )
    with pytest.raises(EmbeddingIdentityMismatchError):
        _ = ProjectRevision(other, None).execute(
            h.world.revision(next(iter(h.world.documents))), generation
        )
    smaller = FakeBgeM3Embeddings(
        identity=SearchEmbeddingIdentity("fake-bge-m3-1024.v1", "fixture-bge-m3", 768)
    )
    with pytest.raises(EmbeddingDimensionMismatchError):
        _ = ProjectRevision(smaller, None).execute(
            h.world.revision(next(iter(h.world.documents))), generation
        )
    with pytest.raises(ChunkerMismatchError):
        _ = ProjectRevision(h.embedder, None).execute(
            h.world.revision(next(iter(h.world.documents))),
            replace(generation, chunk_version="codepoint-1200-200.v1"),
        )


def test_stale_generation_is_never_searched_after_activation() -> None:
    h = search_harness()
    doc = article(PARAGRAPHS)
    h.world.persist(doc)
    old = h.rebuild.execute(h.embedder.identity).generation
    new = h.rebuild.execute(h.embedder.identity).generation
    assert _active(h) == new
    previous = h.generations.get(old.generation_id)
    assert previous is not None
    assert previous.status is GenerationStatus.RETIRED
    assert h.generations.writable() == (new,)
    hits = h.index.lexical_search(new, "Hải Phòng", SCOPE, 5)
    assert {hit.generation_id for hit in hits} == {new.generation_id}


def test_new_revision_reembeds_only_changed_chunks() -> None:
    h = search_harness()
    doc = article(PARAGRAPHS)
    h.world.persist(doc)
    _ = h.rebuild.execute(h.embedder.identity)
    before = len(h.embedder.texts_embedded)

    h.world.persist(
        article(PARAGRAPHS + "\n\nĐoạn mới hoàn toàn.", article_id=doc.article_id)
    )
    _ = h.drain.execute()

    new_texts = h.embedder.texts_embedded[before:]
    assert 0 < len(new_texts) < 3
