"""Regressions for the 2026-10-28 review (R1-R4).

Adapted from the reviewer's reproductions: the interleavings are injected at
``write_revisions`` (the fenced write every projection path now uses).
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import override

import pytest

from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.application.ports.search_index import (
    IndexStatus,
    RevisionWrite,
    SearchIndexError,
    WriteReport,
)
from backend.rag.application.use_cases.search_candidates import IndexedCandidateSource
from backend.rag.application.use_cases.search_projection import ProjectRevision
from backend.rag.application.use_cases.search_sync import RebuildSearchIndex
from backend.rag.domain.outbox import OutboxStatus
from backend.rag.domain.paragraph_chunking import PARAGRAPH_CHUNKER_VERSION
from backend.rag.domain.retrieval import QueryControl, VectorBranchStatus
from backend.rag.domain.search_embedding import (
    BGE_M3_IDENTITY,
    BGE_M3_MODEL,
    SearchEmbeddingIdentity,
)
from backend.rag.domain.search_index import GenerationStatus, IndexGeneration
from backend.rag.evaluation.ollama_bench import judge, parse_output
from backend.rag.evaluation.search_eval.dataset import load_dataset
from backend.rag.infra.search.memory_index import InMemorySearchIndex
from briefing_support import query
from retrieval_support import CALLER
from search_support import SearchHarness, article, search_harness

OLD = "Cảng Hải Phòng tạm đóng, có 42 tàu chờ."
NEW = "Cảng Hải Phòng đã mở lại, còn 7 tàu chờ."


def _hashes(h: SearchHarness, generation: IndexGeneration) -> set[str]:
    return {d.content_hash for d in h.index.revision_digests(generation)}


@dataclass(slots=True)
class _InterleavedIndex(InMemorySearchIndex):
    """Runs ``before`` once, right before the first write after ``armed``."""

    before: list[Callable[[], None]] = field(default_factory=list[Callable[[], None]])
    armed: bool = False

    @override
    def write_revisions(
        self, generation: IndexGeneration, writes: tuple[RevisionWrite, ...]
    ) -> WriteReport:
        if self.armed:
            self.armed = False
            for action in self.before:
                action()
        return InMemorySearchIndex.write_revisions(self, generation, writes)


def _interleaved() -> tuple[SearchHarness, _InterleavedIndex, IndexGeneration]:
    index = _InterleavedIndex()
    h = search_harness(index=index)
    generation = h.rebuild.execute(None).generation
    _ = h.drain.execute()
    return h, index, generation


# --- R1: a late old worker cannot overwrite a newer projection ----------------


def test_late_old_worker_cannot_overwrite_new_revision_after_both_events_done() -> None:
    h, index, generation = _interleaved()
    old = article(OLD)
    new = article(NEW, article_id=old.article_id)
    h.world.persist(old)

    def newer_worker_commits_first() -> None:
        h.world.persist(new)
        _ = h.drain.execute()
        assert _hashes(h, generation) == {new.content_hash}

    index.before.append(newer_worker_commits_first)
    index.armed = True
    report = h.drain.execute()

    assert report.failed == 0
    assert [row.status for row in h.world.rows] == [OutboxStatus.DONE] * len(
        h.world.rows
    ), [(row.event.event_type, row.status, row.last_error) for row in h.world.rows]
    assert _hashes(h, generation) == {new.content_hash}
    assert h.reconcile.execute(deep=True).consistent


def test_late_old_worker_cannot_resurrect_a_tombstoned_article() -> None:
    h, index, generation = _interleaved()
    doc = article(OLD)
    h.world.persist(doc)

    def tombstone_is_applied_first() -> None:
        h.world.tombstone(doc.article_id)
        _ = h.drain.execute()

    index.before.append(tombstone_is_applied_first)
    index.armed = True
    _ = h.drain.execute()

    assert h.index.revision_digests(generation) == ()
    assert h.reconcile.execute(deep=True).consistent


def test_expired_owner_neither_completes_nor_overwrites_after_reclaim() -> None:
    h = search_harness()
    generation = h.rebuild.execute(None).generation
    _ = h.drain.execute()
    old = article(OLD)
    h.world.persist(old)
    (stale_claim,) = h.outbox.claim(10, 60)
    stale_revision = h.world.revision(old.article_id)
    # Lease expires; the event is re-claimed and the article moves on.
    for row in h.world.rows:
        if row.event.event_id == stale_claim.event_id:
            row.status = OutboxStatus.PENDING
    h.world.persist(article(NEW, article_id=old.article_id))
    _ = h.drain.execute()

    # The expired owner finally writes its old state and tries to ack.
    late = h.index.write_revisions(
        generation,
        (
            RevisionWrite(
                old.article_id,
                stale_revision.version,
                h.projector.execute(stale_revision, generation),
            ),
        ),
    )

    assert (late.applied, late.superseded) == (0, 1)
    assert h.outbox.complete([stale_claim]) == 0
    assert h.reconcile.execute(deep=True).consistent


# --- R2: rebuild never activates after a failed or unfinished repair --------


@dataclass(slots=True)
class _RepairIndex(InMemorySearchIndex):
    """After ``armed``: ``status`` runs ``on_status``, then writes misbehave.

    ``mode`` ``fail`` reports every write failed; ``drop`` acknowledges
    writes without applying them (silent loss, so drift never converges).
    """

    on_status: list[Callable[[], None]] = field(
        default_factory=list[Callable[[], None]]
    )
    mode: str = "fail"
    armed: bool = False
    broken: bool = False

    @override
    def status(self, generation: IndexGeneration) -> IndexStatus:
        if self.armed:
            for action in self.on_status:
                action()
            self.broken = True
        return InMemorySearchIndex.status(self, generation)

    @override
    def write_revisions(
        self, generation: IndexGeneration, writes: tuple[RevisionWrite, ...]
    ) -> WriteReport:
        if not self.broken:
            return InMemorySearchIndex.write_revisions(self, generation, writes)
        if self.mode == "fail":
            return WriteReport(0, 0, len(writes), 0, 0)
        return WriteReport(len(writes), 0, 0, 0, 0)


def test_failed_reconcile_must_not_activate_incomplete_generation() -> None:
    index = _RepairIndex()
    h = search_harness(index=index)
    old = article(OLD)
    h.world.persist(old)
    previous = h.rebuild.execute(None).generation
    _ = h.drain.execute()
    index.on_status.append(
        lambda: h.world.persist(article(NEW, article_id=old.article_id))
    )
    index.armed = True

    with pytest.raises(SearchIndexError):
        _ = h.rebuild.execute(None)

    active = h.generations.active()
    assert active is not None
    assert active.generation_id == previous.generation_id
    statuses = [g.status for g in h.world.generations.values()]
    assert statuses.count(GenerationStatus.FAILED) == 1


def test_drift_that_never_converges_keeps_the_previous_generation() -> None:
    index = _RepairIndex(mode="drop")
    h = search_harness(index=index)
    h.world.persist(article(OLD))
    previous = h.rebuild.execute(None).generation
    index.on_status.append(lambda: h.world.persist(article("Bài mới chưa vào index.")))
    index.armed = True

    with pytest.raises(SearchIndexError, match="inconsistent"):
        _ = h.rebuild.execute(None)

    active = h.generations.active()
    assert active is not None
    assert active.generation_id == previous.generation_id


# --- R3: the full embedding fingerprint separates spaces --------------------


@dataclass(slots=True)
class _Encoder:
    revision: str
    axis: int
    calls: int = 0

    @property
    def identity(self) -> SearchEmbeddingIdentity:
        return SearchEmbeddingIdentity(
            BGE_M3_IDENTITY, BGE_M3_MODEL, 1024, revision=self.revision, max_tokens=1024
        )

    def _vector(self) -> tuple[float, ...]:
        return tuple(1.0 if i == self.axis else 0.0 for i in range(1024))

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        self.calls += 1
        return tuple(self._vector() for _ in texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        del text
        return self._vector()


def test_different_embedding_revision_must_not_reuse_old_cache() -> None:
    h = search_harness()
    a, b = _Encoder("revision-A", 0), _Encoder("revision-B", 1)
    doc = article("Cảng Hải Phòng mở lại hôm nay.")
    h.world.persist(doc)
    g1 = h.generations.create(
        PARAGRAPH_CHUNKER_VERSION, a.identity, "eventlens-rag-search"
    )
    g2 = h.generations.create(
        PARAGRAPH_CHUNKER_VERSION, b.identity, "eventlens-rag-search"
    )
    docs_a = ProjectRevision(a, h.cache).execute(h.world.revision(doc.article_id), g1)  # type: ignore[arg-type]
    docs_b = ProjectRevision(b, h.cache).execute(h.world.revision(doc.article_id), g2)  # type: ignore[arg-type]
    assert b.calls == 1
    assert docs_b[0].embedding != docs_a[0].embedding
    assert g1.embedding_identity != g2.embedding_identity


@pytest.mark.parametrize(
    "change",
    [
        {"revision": "other"},
        {"max_tokens": 512},
        {"pooling": "mean"},
        {"normalization": "none"},
        {"input_format": "chunk-only.v1"},
    ],
)
def test_every_output_affecting_setting_changes_the_fingerprint(
    change: dict[str, object],
) -> None:
    base = SearchEmbeddingIdentity(BGE_M3_IDENTITY, BGE_M3_MODEL, 1024, revision="r")
    assert replace(base, **change).key != base.key  # type: ignore[arg-type]


def test_query_from_another_revision_is_refused_at_search_time() -> None:
    h = search_harness()
    h.world.persist(article("Cảng Hải Phòng mở lại hôm nay."))
    encoder_a = _Encoder("revision-A", 0)
    _ = RebuildSearchIndex(
        h.world,
        h.index,
        h.generations,
        ProjectRevision(encoder_a, h.cache),  # type: ignore[arg-type]
    ).execute(encoder_a.identity)
    source = IndexedCandidateSource(
        index=h.index,
        embedder=_Encoder("revision-B", 0),  # type: ignore[arg-type]
        generations=h.generations.active,
    )
    batch = source.candidates(
        "cảng Hải Phòng",
        RetrievalScope((), None, None, None),
        query("cảng Hải Phòng mở lại"),
        CALLER,
        QueryControl(10),
        lexical=True,
        semantic=True,
    )
    assert batch.vector_status is VectorBranchStatus.EMBEDDING_INCOMPATIBLE
    assert batch.lexical  # lexical branch still answers


# --- R4: strict Ollama output validation -----------------------------------


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"abstain": "false", "claims": []}),
        json.dumps({"abstain": False, "claims": []}),
        json.dumps({"abstain": False, "answer": "x", "claims": []}),
        json.dumps(
            {
                "abstain": False,
                "answer": "",
                "claims": [{"text": "x", "sources": ["S1"]}],
            }
        ),
        json.dumps({"abstain": True, "answer": "x", "claims": []}),
        json.dumps(
            {
                "abstain": True,
                "answer": None,
                "claims": [{"text": "x", "sources": ["S1"]}],
            }
        ),
        json.dumps({"abstain": 0, "answer": None, "claims": []}),
        json.dumps(
            {"abstain": False, "answer": "x", "claims": [{"text": "x", "sources": []}]}
        ),
        json.dumps(
            {
                "abstain": False,
                "answer": "x",
                "claims": [{"text": "", "sources": ["S1"]}],
            }
        ),
        json.dumps(
            {"abstain": False, "answer": "x", "claims": [{"text": "x", "sources": [1]}]}
        ),
        json.dumps({"abstain": False, "answer": "x", "claims": "S1"}),
        json.dumps({"abstain": True, "answer": None, "claims": [], "extra": 1}),
        json.dumps([1, 2]),
        "null",
        "not json",
    ],
)
def test_ollama_schema_metric_rejects_malformed_output(content: str) -> None:
    question = next(q for q in load_dataset().questions if q.answerable)
    assert parse_output(content) is None
    judged = judge(question, [], content)
    assert not judged.valid
    assert not judged.abstained
    assert not judged.linked


def test_ollama_schema_metric_accepts_well_formed_output() -> None:
    question = next(q for q in load_dataset().questions if q.answerable)
    abstain = json.dumps({"abstain": True, "answer": None, "claims": []})
    answer = json.dumps(
        {"abstain": False, "answer": "x", "claims": [{"text": "x", "sources": ["S9"]}]}
    )
    assert judge(question, [], abstain).valid
    assert judge(question, [], abstain).abstained
    answered = judge(question, [], answer)
    assert answered.valid
    assert not answered.linked  # S9 does not exist


def test_explicit_write_version_is_stamped_on_chunks_and_never_self_deletes() -> None:
    h = search_harness()
    generation = h.rebuild.execute(None).generation
    doc = article(OLD)
    h.world.persist(doc)
    documents = h.projector.execute(h.world.revision(doc.article_id), generation)
    unversioned = tuple(replace(d, projection_version=0) for d in documents)

    report = h.index.write_revisions(
        generation, (RevisionWrite(doc.article_id, 42, unversioned),)
    )

    assert (report.applied, report.deleted) == (1, 0)
    store = h.index.indices[generation.index_name]
    assert {d.projection_version for d in store.values()} == {42}
