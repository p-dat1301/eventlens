"""Regression tests for T4 independent review findings R1-R4 (2026-10-16).

DB cases run on real PostgreSQL/pgvector, each in a fresh schema. Interleavings
use Events and a connection gate that pauses the reconciler thread right before
it takes the article row lock; no sleeps decide ordering.
"""

from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event, current_thread
from typing import Self, final
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from enrichment_support import (  # pyright: ignore[reportImplicitRelativeImport]
    CountingEmbeddings,
    document,
    identity_of,
)
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow

import eventlens_rag.adapters.postgres_enrichment as pg_enrichment
from eventlens_rag.adapters.connection import connect
from eventlens_rag.adapters.fake_enricher import FakeArticleEnricher
from eventlens_rag.adapters.migrations import migrate
from eventlens_rag.adapters.postgres import PostgresRagStore
from eventlens_rag.adapters.postgres_enrichment import PostgresEnrichmentRepository
from eventlens_rag.adapters.staging import PostgresStaging
from eventlens_rag.adapters.telemetry import RecordingTelemetry
from eventlens_rag.application.enrichment import (
    CancellationScope,
    EnrichmentSteps,
    bundled_retry_policy,
)
from eventlens_rag.application.index import EnsureGeneration
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.domain.enrichment_draft import PromptContext
from eventlens_rag.domain.enrichment_lifecycle import (
    CancelAck,
    FailureCode,
    OutcomeKind,
)
from eventlens_rag.domain.staging import StageRequest
from eventlens_rag.graph.enrichment import EnrichmentRunner
from eventlens_rag.ports.enrichment import EnrichmentRequest
from eventlens_rag.ports.staging import GenerationStaging

FAKE = FakeArticleEnricher()
IDENTITY = identity_of(FAKE)
POLICY = bundled_retry_policy()
RECONCILER = "t4-reconciler"
WAIT = 20


@pytest.fixture
def url(database_url: str) -> Generator[str]:
    schema = "t4fix_" + uuid4().hex
    with psycopg.connect(database_url, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    scoped = make_conninfo(database_url, options=f"-c search_path={schema},public")
    migrate(scoped)
    yield scoped
    with psycopg.connect(database_url, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )


def rows(url: str, query: str, params: tuple[object, ...] = ()) -> list[object]:
    with psycopg.connect(url) as connection:
        return list(connection.execute(query.encode(), params).fetchall())


@final
class _GatedConnection:
    """Pause the reconciler thread once, right before its article row lock."""

    def __init__(
        self, inner: psycopg.Connection[TupleRow], gate: Callable[[str], None]
    ) -> None:
        self._inner = inner
        self._gate = gate

    def __enter__(self) -> Self:
        _ = self._inner.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._inner.__exit__(None, None, None)

    def execute(self, query: str, params: object = None) -> object:
        self._gate(query)
        return self._inner.execute(query, params)  # pyright: ignore[reportArgumentType]

    def cursor(self, *args: object, **kwargs: object) -> object:
        return self._inner.cursor(*args, **kwargs)


def _pause_reconciler_before_article_lock(
    paused: Event, release: Event
) -> Callable[[str], object]:
    real_connect = connect
    fired: list[bool] = []

    def gate(query: str) -> None:
        if (
            current_thread().name.startswith(RECONCILER)
            and not fired
            and "rag_article_current" in query
            and "FOR UPDATE" in query
        ):
            fired.append(True)
            paused.set()
            assert release.wait(WAIT)

    def gated_connect(database_url: str) -> object:
        return _GatedConnection(real_connect(database_url), gate)

    return gated_connect


def test_r1_obsolete_reconciliation_candidate_cannot_strand_current_revision(
    url: str,
) -> None:
    repository = PostgresEnrichmentRepository(url)
    old = document("Old Harbor Authority report")
    new = document("Corrected Harbor Authority report", article_id=old.article_id)
    PostgresRagStore(url).persist_article(old)
    paused, release = Event(), Event()
    gated = _pause_reconciler_before_article_lock(paused, release)
    with (
        patch.object(pg_enrichment, "connect", gated),
        ThreadPoolExecutor(1, thread_name_prefix=RECONCILER) as pool,
    ):
        future = pool.submit(repository.enqueue_missing, IDENTITY, 25)
        assert paused.wait(WAIT)
        try:
            receipt = repository.ingest(
                new, IDENTITY.key_for(new.article_id, new.content_hash)
            )
        finally:
            release.set()
        _ = future.result(timeout=WAIT)
    # The corrected revision's job is still live and claimable.
    assert rows(
        url,
        "SELECT status FROM rag_enrichment_jobs WHERE job_id = %s",
        (receipt.job_id,),
    ) == [("PENDING",)]
    assert rows(
        url,
        "SELECT count(*) FROM rag_enrichment_jobs WHERE content_hash = %s",
        (old.content_hash,),
    ) == [(0,)]
    claim = repository.claim_next(IDENTITY, POLICY)
    assert claim is not None
    assert claim.key.content_hash == new.content_hash


def test_r1_reconciliation_enqueues_the_current_revision_not_the_candidate(
    url: str,
) -> None:
    repository = PostgresEnrichmentRepository(url)
    store = PostgresRagStore(url)
    old = document("Old Harbor Authority report")
    new = document("Corrected Harbor Authority report", article_id=old.article_id)
    store.persist_article(old)
    paused, release = Event(), Event()
    gated = _pause_reconciler_before_article_lock(paused, release)
    with (
        patch.object(pg_enrichment, "connect", gated),
        ThreadPoolExecutor(1, thread_name_prefix=RECONCILER) as pool,
    ):
        future = pool.submit(repository.enqueue_missing, IDENTITY, 25)
        assert paused.wait(WAIT)
        try:
            store.persist_article(new)  # correction persisted without a job
        finally:
            release.set()
        _ = future.result(timeout=WAIT)
    assert rows(url, "SELECT content_hash, status FROM rag_enrichment_jobs") == [
        (new.content_hash, "PENDING")
    ]


def test_r2_concurrent_reconciliation_never_resets_budget_or_backoff(
    url: str,
) -> None:
    repository = PostgresEnrichmentRepository(url)
    article = document("Harbor Authority retry race")
    PostgresRagStore(url).persist_article(article)
    paused, release = Event(), Event()
    gated = _pause_reconciler_before_article_lock(paused, release)
    state = """SELECT attempt_count, failure_code, next_attempt_at > clock_timestamp(),
        claim_generation FROM rag_enrichment_jobs"""
    with (
        patch.object(pg_enrichment, "connect", gated),
        ThreadPoolExecutor(1, thread_name_prefix=RECONCILER) as pool,
    ):
        future = pool.submit(repository.enqueue_missing, IDENTITY, 25)
        assert paused.wait(WAIT)
        try:
            _ = repository.ingest(
                article, IDENTITY.key_for(article.article_id, article.content_hash)
            )
            claim = repository.claim_next(IDENTITY, POLICY)
            assert claim is not None
            assert repository.schedule_retry(claim, FailureCode.PROVIDER_TIMEOUT, 30)
            before = rows(url, state)
        finally:
            release.set()
        _ = future.result(timeout=WAIT)
    assert before == [(1, "provider_timeout", True, 1)]
    assert rows(url, state) == before
    assert repository.claim_next(IDENTITY, POLICY) is None


def test_r2_duplicate_ingest_preserves_scheduled_retry(url: str) -> None:
    repository = PostgresEnrichmentRepository(url)
    article = document("Harbor Authority duplicate delivery")
    key = IDENTITY.key_for(article.article_id, article.content_hash)
    _ = repository.ingest(article, key)
    claim = repository.claim_next(IDENTITY, POLICY)
    assert claim is not None
    assert repository.schedule_retry(claim, FailureCode.PROVIDER_TIMEOUT, 30)
    _ = repository.ingest(article, key)
    assert repository.enqueue_missing(IDENTITY, 25) == 0
    assert rows(
        url,
        """SELECT status, attempt_count, failure_code,
        next_attempt_at > clock_timestamp() FROM rag_enrichment_jobs""",
    ) == [("PENDING", 1, "provider_timeout", True)]


def _runner(
    url: str, staging: GenerationStaging, embeddings: CountingEmbeddings
) -> tuple[EnrichmentRunner, PostgresEnrichmentRepository]:
    repository = PostgresEnrichmentRepository(url)
    steps = EnrichmentSteps(
        repository,
        FAKE,
        EnsureGeneration(staging, embeddings, EmbeddingIdentity()),
        POLICY,
        lambda: datetime.now(UTC),
    )
    return EnrichmentRunner(steps, RecordingTelemetry()), repository


def test_r3_cancel_acknowledged_inside_stage_starts_no_embedding_or_rows(
    url: str,
) -> None:
    scope = CancellationScope()
    reached, release = Event(), Event()
    inner = PostgresStaging(url)
    stage_calls: list[bool] = []

    class PausedStaging:
        def find_complete(
            self, article: ArticleDocument, identity: EmbeddingIdentity
        ) -> int | None:
            found = inner.find_complete(article, identity)
            reached.set()
            assert release.wait(WAIT)
            return found

        def stage(self, request: StageRequest) -> int:
            stage_calls.append(scope.stopped)
            return inner.stage(request)

    embeddings = CountingEmbeddings()
    worker, repository = _runner(url, PausedStaging(), embeddings)
    article = document("Harbor Authority cancellation race")
    _ = repository.ingest(
        article, IDENTITY.key_for(article.article_id, article.content_hash)
    )
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker.run, 1, scope)
        assert reached.wait(WAIT)
        try:
            ack = scope.cancel()
        finally:
            release.set()
        report = future.result(timeout=WAIT)
    assert ack is CancelAck.ACKNOWLEDGED
    assert [o.kind for o in report.outcomes] == [OutcomeKind.CANCELLED]
    assert embeddings.calls == 0
    assert stage_calls == []
    for table in ("rag_index_generations", "rag_chunks", "rag_enrichment_publications"):
        assert rows(url, f"SELECT count(*) FROM {table}") == [(0,)]  # noqa: S608
    assert rows(url, "SELECT status, failure_code FROM rag_enrichment_jobs") == [
        ("PROCESSING", None)
    ]


def test_r3_already_cancelled_run_does_no_reconcile_or_claim(url: str) -> None:
    worker, _ = _runner(url, PostgresStaging(url), CountingEmbeddings())
    PostgresRagStore(url).persist_article(document("Harbor Authority cancelled"))
    scope = CancellationScope()
    assert scope.cancel() is None
    report = worker.run(1, scope)
    assert report.cancelled
    assert report.reconciled == 0
    assert report.outcomes == ()
    assert rows(url, "SELECT count(*) FROM rag_enrichment_jobs") == [(0,)]


def _fake_request(content: str) -> EnrichmentRequest:
    article = document(content)
    key = IDENTITY.key_for(article.article_id, article.content_hash)
    return EnrichmentRequest(PromptContext.from_document(article), key, 30)


@pytest.mark.parametrize(
    "content",
    [
        "Straße harbor",
        "STRASSE Straße ﬁre near the HARBOR",
        "İstanbul harbor reopened",
        "q̇ storm at the harbor",
        "ǅ ß İ ﬃ Harbor storm",
    ],
)
def test_r4_fake_offsets_are_code_points_of_the_original_text(content: str) -> None:
    draft = FAKE.enrich(_fake_request(content))
    assert draft.evidence
    for span in draft.evidence:
        assert content[span.start_offset : span.end_offset] == span.excerpt
        assert span.excerpt.casefold() in {"harbor", "storm", "fire"}


def test_r4_valid_unicode_article_publishes_on_real_db(url: str) -> None:
    worker, repository = _runner(url, PostgresStaging(url), CountingEmbeddings())
    article = document("Straße harbor")
    _ = repository.ingest(
        article, IDENTITY.key_for(article.article_id, article.content_hash)
    )
    (outcome,) = worker.run(1).outcomes
    assert outcome.kind is OutcomeKind.READY
    (excerpt,) = rows(
        url,
        """SELECT span->>'excerpt' FROM rag_enrichment_versions,
        jsonb_array_elements(metadata->'evidence_spans') span""",
    )
    assert excerpt == ("harbor",)
