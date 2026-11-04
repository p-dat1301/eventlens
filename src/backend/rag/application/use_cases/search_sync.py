"""Keep the search index in step with PostgreSQL: drain, rebuild, reconcile.

* ``DrainOutbox`` applies claimed outbox events to every writable generation.
  An event never carries the document: the worker re-reads the article's
  *current* state from PostgreSQL, so replays and out-of-order delivery are
  idempotent and an old revision can never be re-indexed. Events are marked
  DONE only after the index acknowledged every write; otherwise they retry
  with backoff and end FAILED (dead letter) once the budget is spent.
* ``RebuildSearchIndex`` builds a new generation from PostgreSQL pages and
  activates it only when complete; an interrupted build is marked FAILED and
  the previous ACTIVE generation keeps serving.
* ``ReconcileSearchIndex`` compares PostgreSQL current revisions with what the
  index holds and can repair the difference.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from backend.rag.application.ports.search_index import (
    RevisionWrite,
    SearchHealth,
    SearchIndexError,
    SearchIndexPort,
)
from backend.rag.application.ports.search_sync import (
    OutboxStore,
    ProjectionRevision,
    SearchGenerationStore,
    SearchProjectionSource,
)
from backend.rag.application.use_cases.search_projection import (
    ProjectionStats,
    ProjectRevision,
    chunk_count,
)
from backend.rag.domain.outbox import (
    DEFAULT_OUTBOX_POLICY,
    DrainReport,
    OutboxEvent,
    OutboxEventType,
    OutboxRetryPolicy,
    OutboxStatus,
)
from backend.rag.domain.search_embedding import SearchEmbeddingIdentity
from backend.rag.domain.search_index import (
    IndexGeneration,
    RevisionDigest,
    SearchChunkDocument,
)

ARTICLE_EVENTS: Final = frozenset(
    {
        OutboxEventType.ARTICLE_REVISION_CREATED,
        OutboxEventType.ARTICLE_REVISION_UPDATED,
        OutboxEventType.ARTICLE_TOMBSTONED,
        OutboxEventType.ENRICHMENT_PUBLISHED,
    }
)
REBUILD_PAGE: Final = 200
INDEX_PREFIX: Final = "eventlens-rag-search"


def _require(condition: bool, message: str) -> None:  # noqa: FBT001
    if not condition:
        raise SearchIndexError(message)


def apply_revision(
    index: SearchIndexPort,
    projector: ProjectRevision,
    revision: ProjectionRevision,
    generation: IndexGeneration,
) -> None:
    """Fenced write of one article's current state; raises unless acknowledged.

    A superseded write (the index already holds a newer version of the
    article) is success: the newer state's own event/write owns the result.
    """
    documents: tuple[SearchChunkDocument, ...] = ()
    if revision.document is not None and not revision.tombstoned:
        documents = projector.execute(revision, generation)
    if not documents:
        # Raises when the fenced delete is refused.
        _ = index.delete_article(
            generation, revision.article_id, version=revision.version
        )
        return
    # The fence version travels on the chunks (projection_version).
    result = index.upsert_revision(generation, revision.article_id, documents)
    _require(not result.failed, "index refused part of the revision")


def write_page(
    index: SearchIndexPort,
    generation: IndexGeneration,
    page: tuple[ProjectionRevision, ...],
    documents: tuple[SearchChunkDocument, ...],
) -> int:
    """Fenced batch write of one rebuild page; returns documents indexed."""
    by_article: dict[UUID, list[SearchChunkDocument]] = {}
    for document in documents:
        by_article.setdefault(document.article_id, []).append(document)
    writes = tuple(
        RevisionWrite(
            revision.article_id,
            revision.version,
            tuple(by_article.get(revision.article_id, ())),
        )
        for revision in page
    )
    report = index.write_revisions(generation, writes)
    _require(not report.failed, "index refused part of the rebuild")
    _require(
        report.applied + report.superseded == len(writes),
        "index did not acknowledge every article of the page",
    )
    return report.indexed


def _error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) else type(error).__name__


@dataclass(slots=True)
class DrainOutbox:
    """Apply outbox events to the index (idempotent, at-least-once)."""

    outbox: OutboxStore
    source: SearchProjectionSource
    index: SearchIndexPort
    generations: SearchGenerationStore
    projector: ProjectRevision
    policy: OutboxRetryPolicy = DEFAULT_OUTBOX_POLICY

    def execute(self, max_batches: int = 10) -> DrainReport:
        """Drain up to ``max_batches`` batches; returns cumulative counts."""
        totals = DrainReport(0, 0, 0, 0, 0)
        for _ in range(max_batches):
            report = self._batch()
            totals = DrainReport(
                claimed=totals.claimed + report.claimed,
                done=totals.done + report.done,
                retried=totals.retried + report.retried,
                failed=totals.failed + report.failed,
                skipped_stale=totals.skipped_stale + report.skipped_stale,
            )
            if report.claimed < self.policy.batch_size:
                break
        return totals

    def _batch(self) -> DrainReport:
        targets = self.generations.writable()
        if not targets:
            # Nothing to project into: leave events PENDING for the first build.
            return DrainReport(0, 0, 0, 0, 0)
        events = self.outbox.claim(self.policy.batch_size, self.policy.lease_seconds)
        by_article: dict[UUID, list[OutboxEvent]] = {}
        other: list[OutboxEvent] = []
        for event in events:
            if event.event_type in ARTICLE_EVENTS and event.article_id is not None:
                by_article.setdefault(event.article_id, []).append(event)
            else:
                other.append(event)
        done = 0
        retried = 0
        failed = 0
        # Generation activation needs no per-article work: rebuild projected it.
        done += self.outbox.complete(other)
        for article_id, article_events in by_article.items():
            try:
                revision = self.source.revision(article_id)
                for generation in targets:
                    self._apply(revision, generation)
            except Exception as error:  # noqa: BLE001 - every failure is retried
                code = _error_code(error)
                for event in article_events:
                    delay = self.policy.delay_after(event.attempts)
                    if delay is None:
                        failed += int(self.outbox.fail(event, code))
                    else:
                        retried += int(self.outbox.retry(event, code, delay))
                continue
            done += self.outbox.complete(article_events)
        return DrainReport(len(events), done, retried, failed, 0)

    def _apply(self, revision: ProjectionRevision, generation: IndexGeneration) -> None:
        apply_revision(self.index, self.projector, revision, generation)


@dataclass(frozen=True, slots=True)
class RebuildReport:
    """Outcome of one full rebuild."""

    generation: IndexGeneration
    revisions: int
    chunks: int
    embedded: int
    cache_hits: int
    indexed: int
    latency_ms: float
    previous: IndexGeneration | None


@dataclass(slots=True)
class RebuildSearchIndex:
    """Build a fresh generation from PostgreSQL, then activate it atomically."""

    source: SearchProjectionSource
    index: SearchIndexPort
    generations: SearchGenerationStore
    projector: ProjectRevision
    page_size: int = REBUILD_PAGE
    timer: Callable[[], float] = time.perf_counter
    index_prefix: str = INDEX_PREFIX
    drop_previous: bool = False
    repair_rounds: int = 3

    def execute(self, embedding: SearchEmbeddingIdentity | None) -> RebuildReport:
        """Project every current revision; FAILED generation on any error."""
        started = self.timer()
        previous = self.generations.active()
        generation = self.generations.create(
            self.projector.policy.version, embedding, self.index_prefix
        )
        self.projector.stats = ProjectionStats()
        try:
            indexed = self._build(generation)
            self._repair_until_consistent(generation)
        except BaseException:
            self.generations.mark_failed(generation.generation_id)
            raise
        active = self.generations.activate(generation.generation_id)
        if self.drop_previous and previous is not None:
            self.index.drop_index(previous)
        stats = self.projector.stats
        return RebuildReport(
            generation=active,
            revisions=stats.revisions,
            chunks=stats.chunks,
            embedded=stats.embedded,
            cache_hits=stats.cache_hits,
            indexed=indexed,
            latency_ms=(self.timer() - started) * 1000.0,
            previous=previous,
        )

    def _repair_until_consistent(self, generation: IndexGeneration) -> None:
        """Repair writes that raced the page reads; activation needs a clean check.

        Each round repairs (any refused write raises) and re-checks. Writes
        concurrent with the rebuild also reach this BUILDING generation through
        the outbox, so a few rounds converge; still-drifting state after
        ``repair_rounds`` raises and the previous ACTIVE generation stays.
        """
        reconcile = ReconcileSearchIndex(
            self.source, self.index, self.generations, self.projector
        )
        for _ in range(self.repair_rounds):
            check = reconcile.execute(deep=True, generation=generation)
            if check.consistent:
                return
            _ = reconcile.execute(repair=True, deep=True, generation=generation)
        final = reconcile.execute(deep=True, generation=generation)
        _require(final.consistent, "generation still inconsistent after repair")

    def _build(self, generation: IndexGeneration) -> int:
        """Index every current revision page by page; returns documents indexed."""
        indexed = 0
        self.index.ensure_index(generation)
        after: UUID | None = None
        while page := self.source.current_page(after, self.page_size):
            documents = self.projector.execute_many(page, generation)
            indexed += write_page(self.index, generation, page, documents)
            after = page[-1].article_id
        status = self.index.status(generation)
        _require(
            status.documents >= indexed,
            "index holds fewer documents than were acknowledged",
        )
        return indexed


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Drift between PostgreSQL (truth) and one index generation."""

    generation: IndexGeneration
    postgres_revisions: int
    index_revisions: int
    missing: tuple[UUID, ...]
    stale: tuple[UUID, ...]
    extra: tuple[UUID, ...]
    chunk_mismatch: tuple[UUID, ...]
    repaired: int

    @property
    def consistent(self) -> bool:
        """True when nothing is missing, stale, extra or mis-chunked."""
        return not (self.missing or self.stale or self.extra or self.chunk_mismatch)


@dataclass(slots=True)
class ReconcileSearchIndex:
    """Detect (and optionally repair) PostgreSQL vs index drift."""

    source: SearchProjectionSource
    index: SearchIndexPort
    generations: SearchGenerationStore
    projector: ProjectRevision
    page_size: int = REBUILD_PAGE

    def execute(
        self,
        *,
        repair: bool = False,
        deep: bool = False,
        generation: IndexGeneration | None = None,
    ) -> ReconcileReport:
        """Compare revisions (and, with ``deep``, chunk counts)."""
        generation = generation or self.generations.active()
        if generation is None:
            msg = "no ACTIVE search generation"
            raise SearchIndexError(msg)
        truth = {d.article_id: d.content_hash for d in self.source.current_digests()}
        held: dict[UUID, list[RevisionDigest]] = {}
        for digest in self.index.revision_digests(generation):
            held.setdefault(digest.article_id, []).append(digest)
        missing = tuple(sorted((a for a in truth if a not in held), key=str))
        stale = tuple(
            sorted(
                (
                    a
                    for a in truth
                    if a in held and [d.content_hash for d in held[a]] != [truth[a]]
                ),
                key=str,
            )
        )
        extra = tuple(sorted((a for a in held if a not in truth), key=str))
        mismatch: list[UUID] = []
        if deep:
            single = {a: ds[0] for a, ds in held.items() if len(ds) == 1}
            mismatch = self._chunk_mismatch(single, set(missing) | set(stale))
        repaired = 0
        if repair:
            repaired = self._repair(generation, (*missing, *stale, *mismatch), extra)
        return ReconcileReport(
            generation=generation,
            postgres_revisions=len(truth),
            index_revisions=sum(len(ds) for ds in held.values()),
            missing=missing,
            stale=stale,
            extra=extra,
            chunk_mismatch=tuple(mismatch),
            repaired=repaired,
        )

    def _chunk_mismatch(
        self, digests: dict[UUID, RevisionDigest], skip: set[UUID]
    ) -> list[UUID]:
        found: list[UUID] = []
        after: UUID | None = None
        while True:
            page = self.source.current_page(after, self.page_size)
            if not page:
                break
            for revision in page:
                document = revision.document
                digest = digests.get(revision.article_id)
                if document is None or digest is None or revision.article_id in skip:
                    continue
                expected = chunk_count(
                    document.normalized_content, self.projector.policy
                )
                if digest.chunks != expected:
                    found.append(revision.article_id)
            after = page[-1].article_id
        return found

    def _repair(
        self,
        generation: IndexGeneration,
        refresh: tuple[UUID, ...],
        extra: tuple[UUID, ...],
    ) -> int:
        repaired = 0
        for article_id in (*refresh, *extra):
            # Re-read the current state (and its version) right before the
            # fenced write; a failed write raises, never counts as repaired.
            revision = self.source.revision(article_id)
            apply_revision(self.index, self.projector, revision, generation)
            repaired += 1
        return repaired


@dataclass(frozen=True, slots=True)
class SearchHealthReport:
    """Engine, generation and outbox health in one view."""

    engine: SearchHealth
    generation: IndexGeneration | None
    outbox: dict[OutboxStatus, int] = field(default_factory=dict[OutboxStatus, int])
    oldest_pending_age_seconds: float | None = None

    @property
    def healthy(self) -> bool:
        """Reachable, an ACTIVE generation, its index present, no dead letters."""
        return (
            self.engine.reachable
            and self.generation is not None
            and self.engine.index_exists
            and self.outbox.get(OutboxStatus.FAILED, 0) == 0
        )


@dataclass(frozen=True, slots=True)
class SearchIndexHealthCheck:
    """Read-only health: never raises for an unreachable engine."""

    index: SearchIndexPort
    generations: SearchGenerationStore
    outbox: OutboxStore
    now: Callable[[], float] = time.time

    def execute(self) -> SearchHealthReport:
        """Collect engine, generation and outbox state."""
        generation = self.generations.active()
        engine = self.index.health(generation)
        oldest = self.outbox.oldest_pending()
        return SearchHealthReport(
            engine=engine,
            generation=generation,
            outbox=self.outbox.counts(),
            oldest_pending_age_seconds=(
                None if oldest is None else max(0.0, self.now() - oldest.timestamp())
            ),
        )
