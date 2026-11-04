"""In-memory PostgreSQL-side fakes for the search platform (unit tests only).

``MemorySearchWorld`` mimics the revision-0002 triggers: every persist,
correction, tombstone and publication appends an outbox event in the same
step as the data change. DB tests prove the real triggers separately.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from backend.rag.application.ports.search_sync import ProjectionRevision
from backend.rag.application.use_cases.search_projection import ProjectRevision
from backend.rag.application.use_cases.search_sync import (
    DrainOutbox,
    RebuildSearchIndex,
    ReconcileSearchIndex,
)
from backend.rag.domain.article_document import (
    ArticleDocument,
    normalized_content_hash,
)
from backend.rag.domain.outbox import (
    OutboxEvent,
    OutboxEventType,
    OutboxRetryPolicy,
    OutboxStatus,
)
from backend.rag.domain.search_embedding import SearchEmbeddingIdentity
from backend.rag.domain.search_index import (
    GenerationStatus,
    IndexGeneration,
    RevisionDigest,
)
from backend.rag.infra.embeddings.fake_bge_m3 import FakeBgeM3Embeddings
from backend.rag.infra.search.memory_index import InMemorySearchIndex
from backend.shared.contracts import ArticleEnrichment

START = datetime(2026, 3, 1, tzinfo=UTC)
SOURCE_ID = UUID("00000000-0000-4000-8000-0000000000aa")


def article(  # noqa: PLR0913 - explicit test builder
    content: str,
    *,
    article_id: UUID | None = None,
    title: str = "Bản tin cảng Hải Phòng",
    domain: str = "fixture.example",
    published: datetime | None = START,
    source_name: str = "Fixture Wire",
) -> ArticleDocument:
    """Valid ArticleDocument with NFC content."""
    digest = normalized_content_hash(content)
    return ArticleDocument.model_validate(
        {
            "schema_version": "article_document.v1",
            "article_id": article_id or uuid4(),
            "source_id": SOURCE_ID,
            "title": title,
            "raw_content": content,
            "normalization_version": "nfc.v1",
            "normalized_content": content,
            "canonical_url": "https://fixture.example/a",
            "source_name": source_name,
            "source_domain": domain,
            "source_published_at": published,
            "ingested_at": START,
            "processed_at": START,
            "content_hash": digest,
            "content_fingerprint": digest,
            "visibility": "internal",
            "evidence_spans": (),
        }
    )


@dataclass(slots=True)
class _Row:
    event: OutboxEvent
    status: OutboxStatus = OutboxStatus.PENDING
    next_at: datetime = START
    last_error: str | None = None


@dataclass(slots=True)
class MemorySearchWorld:
    """Documents, current pointers, metadata, outbox, generations, cache."""

    documents: dict[UUID, ArticleDocument] = field(
        default_factory=dict[UUID, ArticleDocument]
    )
    tombstoned: set[UUID] = field(default_factory=set[UUID])
    metadata: dict[UUID, ArticleEnrichment] = field(
        default_factory=dict[UUID, ArticleEnrichment]
    )
    rows: list[_Row] = field(default_factory=list[_Row])
    generations: dict[int, IndexGeneration] = field(
        default_factory=dict[int, IndexGeneration]
    )
    cache: dict[tuple[str, str], tuple[float, ...]] = field(
        default_factory=dict[tuple[str, str], tuple[float, ...]]
    )
    now: datetime = START

    # --- writes that fire "triggers" ------------------------------------
    def _emit(self, kind: OutboxEventType, item: ArticleDocument | None) -> None:
        self.rows.append(
            _Row(
                OutboxEvent(
                    event_id=len(self.rows) + 1,
                    event_type=kind,
                    article_id=None if item is None else item.article_id,
                    content_hash=None if item is None else item.content_hash,
                    generation_id=None,
                    attempts=0,
                    claim_token=uuid4(),
                    created_at=self.now,
                ),
                next_at=self.now,
            )
        )

    def persist(self, document: ArticleDocument) -> None:
        """Insert or correct the current revision (+ outbox event)."""
        old = self.documents.get(document.article_id)
        if document.article_id in self.tombstoned:
            return
        self.documents[document.article_id] = document
        if old is None:
            self._emit(OutboxEventType.ARTICLE_REVISION_CREATED, document)
        elif old.content_hash != document.content_hash:
            _ = self.metadata.pop(document.article_id, None)
            self._emit(OutboxEventType.ARTICLE_REVISION_UPDATED, document)

    def tombstone(self, article_id: UUID) -> None:
        """Tombstone (+ outbox event)."""
        self.tombstoned.add(article_id)
        self._emit(OutboxEventType.ARTICLE_TOMBSTONED, self.documents.get(article_id))

    def publish(self, metadata: ArticleEnrichment) -> None:
        """READY metadata for the current revision (+ outbox event)."""
        self.metadata[metadata.article_id] = metadata
        self._emit(
            OutboxEventType.ENRICHMENT_PUBLISHED, self.documents[metadata.article_id]
        )

    # --- SearchProjectionSource ------------------------------------------
    def revision(self, article_id: UUID) -> ProjectionRevision:
        document = self.documents.get(article_id)
        return ProjectionRevision(
            article_id=article_id,
            document=document,
            tombstoned=article_id in self.tombstoned,
            metadata=self.metadata.get(article_id),
            version=max(
                (
                    row.event.event_id
                    for row in self.rows
                    if row.event.article_id == article_id
                ),
                default=0,
            ),
        )

    def current_page(
        self, after: UUID | None, limit: int
    ) -> tuple[ProjectionRevision, ...]:
        ids = sorted((a for a in self.documents if a not in self.tombstoned), key=str)
        if after is not None:
            ids = [a for a in ids if str(a) > str(after)]
        return tuple(self.revision(a) for a in ids[:limit])

    def current_digests(self) -> tuple[RevisionDigest, ...]:
        return tuple(
            RevisionDigest(a, d.content_hash, -1)
            for a, d in sorted(self.documents.items(), key=lambda kv: str(kv[0]))
            if a not in self.tombstoned
        )


@dataclass(slots=True)
class MemoryOutbox:
    """``OutboxStore`` over the world's rows (claim tokens fenced)."""

    world: MemorySearchWorld

    def claim(self, limit: int, lease_seconds: int) -> tuple[OutboxEvent, ...]:
        del lease_seconds
        claimed: list[OutboxEvent] = []
        for row in self.world.rows:
            if len(claimed) >= limit:
                break
            if row.status is OutboxStatus.PENDING and row.next_at <= self.world.now:
                row.event = replace(
                    row.event, attempts=row.event.attempts + 1, claim_token=uuid4()
                )
                row.status = OutboxStatus.PROCESSING
                claimed.append(row.event)
        return tuple(claimed)

    def _row(self, event: OutboxEvent) -> _Row | None:
        for row in self.world.rows:
            if (
                row.event.event_id == event.event_id
                and row.event.claim_token == event.claim_token
                and row.status is OutboxStatus.PROCESSING
            ):
                return row
        return None

    def complete(self, events: Sequence[OutboxEvent]) -> int:
        done = 0
        for event in events:
            row = self._row(event)
            if row is not None:
                row.status = OutboxStatus.DONE
                done += 1
        return done

    def retry(self, event: OutboxEvent, error_code: str, delay_seconds: float) -> bool:
        row = self._row(event)
        if row is None:
            return False
        row.status = OutboxStatus.PENDING
        row.last_error = error_code
        row.next_at = self.world.now + timedelta(seconds=delay_seconds)
        return True

    def fail(self, event: OutboxEvent, error_code: str) -> bool:
        row = self._row(event)
        if row is None:
            return False
        row.status = OutboxStatus.FAILED
        row.last_error = error_code
        return True

    def counts(self) -> dict[OutboxStatus, int]:
        return {
            status: sum(1 for row in self.world.rows if row.status is status)
            for status in OutboxStatus
        }

    def oldest_pending(self) -> datetime | None:
        pending = [
            r.event.created_at
            for r in self.world.rows
            if r.status is OutboxStatus.PENDING
        ]
        return min(pending, default=None)

    def requeue_failed(self) -> int:
        count = 0
        for row in self.world.rows:
            if row.status is OutboxStatus.FAILED:
                row.status = OutboxStatus.PENDING
                row.event = replace(row.event, attempts=0)
                count += 1
        return count


@dataclass(slots=True)
class MemoryGenerations:
    """``SearchGenerationStore`` over the world's generations."""

    world: MemorySearchWorld

    def active(self) -> IndexGeneration | None:
        return next(
            (
                g
                for g in self.world.generations.values()
                if g.status is GenerationStatus.ACTIVE
            ),
            None,
        )

    def writable(self) -> tuple[IndexGeneration, ...]:
        return tuple(
            g
            for g in self.world.generations.values()
            if g.status in {GenerationStatus.ACTIVE, GenerationStatus.BUILDING}
        )

    def get(self, generation_id: int) -> IndexGeneration | None:
        return self.world.generations.get(generation_id)

    def create(
        self,
        chunk_version: str,
        embedding: SearchEmbeddingIdentity | None,
        index_prefix: str,
    ) -> IndexGeneration:
        gid = len(self.world.generations) + 1
        generation = IndexGeneration(
            generation_id=gid,
            index_name=f"{index_prefix}-g{gid}",
            chunk_version=chunk_version,
            embedding_identity=None if embedding is None else embedding.key,
            embedding_model=None if embedding is None else embedding.model,
            embedding_dimensions=None if embedding is None else embedding.dimensions,
            status=GenerationStatus.BUILDING,
        )
        self.world.generations[gid] = generation
        return generation

    def activate(self, generation_id: int) -> IndexGeneration:
        target = self.world.generations[generation_id]
        if target.status is not GenerationStatus.BUILDING:
            msg = "not BUILDING"
            raise RuntimeError(msg)
        for gid, generation in list(self.world.generations.items()):
            if generation.status is GenerationStatus.ACTIVE:
                self.world.generations[gid] = replace(
                    generation, status=GenerationStatus.RETIRED
                )
        active = replace(target, status=GenerationStatus.ACTIVE)
        self.world.generations[generation_id] = active
        self.world.rows.append(
            _Row(
                OutboxEvent(
                    event_id=len(self.world.rows) + 1,
                    event_type=OutboxEventType.EMBEDDING_GENERATION_ACTIVATED,
                    article_id=None,
                    content_hash=None,
                    generation_id=generation_id,
                    attempts=0,
                    claim_token=uuid4(),
                    created_at=self.world.now,
                ),
                next_at=self.world.now,
            )
        )
        return active

    def mark_failed(self, generation_id: int) -> None:
        generation = self.world.generations[generation_id]
        if generation.status is GenerationStatus.BUILDING:
            self.world.generations[generation_id] = replace(
                generation, status=GenerationStatus.FAILED
            )


@dataclass(slots=True)
class MemoryEmbeddingCache:
    """``EmbeddingCache`` over the world's cache dict."""

    world: MemorySearchWorld
    gets: int = 0
    puts: int = 0

    def get_many(
        self, text_hashes: Sequence[str], identity: SearchEmbeddingIdentity
    ) -> dict[str, tuple[float, ...]]:
        self.gets += 1
        return {
            key: self.world.cache[(key, identity.key)]
            for key in text_hashes
            if (key, identity.key) in self.world.cache
        }

    def put_many(
        self,
        vectors: Sequence[tuple[str, tuple[float, ...]]],
        identity: SearchEmbeddingIdentity,
    ) -> int:
        self.puts += 1
        written = 0
        for key, values in vectors:
            if (key, identity.key) not in self.world.cache:
                self.world.cache[(key, identity.key)] = values
                written += 1
        return written


@dataclass(slots=True)
class SearchHarness:
    """World + adapters + use cases wired like ``build_search_runtime``."""

    world: MemorySearchWorld
    index: InMemorySearchIndex
    embedder: FakeBgeM3Embeddings
    outbox: MemoryOutbox
    generations: MemoryGenerations
    cache: MemoryEmbeddingCache
    projector: ProjectRevision
    drain: DrainOutbox
    rebuild: RebuildSearchIndex
    reconcile: ReconcileSearchIndex


def search_harness(
    *,
    policy: OutboxRetryPolicy | None = None,
    embedder: FakeBgeM3Embeddings | None = None,
    index: InMemorySearchIndex | None = None,
    world: MemorySearchWorld | None = None,
) -> SearchHarness:
    """Fresh in-memory search platform (index/world injectable for interleaving)."""
    world = world or MemorySearchWorld()
    index = index or InMemorySearchIndex()
    embedder = embedder or FakeBgeM3Embeddings()
    outbox = MemoryOutbox(world)
    generations = MemoryGenerations(world)
    cache = MemoryEmbeddingCache(world)
    projector = ProjectRevision(embedder, cache)
    drain = DrainOutbox(
        outbox,
        world,
        index,
        generations,
        projector,
        policy or OutboxRetryPolicy(),
    )
    return SearchHarness(
        world=world,
        index=index,
        embedder=embedder,
        outbox=outbox,
        generations=generations,
        cache=cache,
        projector=projector,
        drain=drain,
        rebuild=RebuildSearchIndex(world, index, generations, projector),
        reconcile=ReconcileSearchIndex(world, index, generations, projector),
    )
