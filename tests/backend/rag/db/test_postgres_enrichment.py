"""Real PostgreSQL/pgvector T4 lifecycle tests (each test owns a fresh schema)."""

from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Event, Thread
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.errors import RaiseException
from pydantic import TypeAdapter

from backend.rag.application.ports.enrichment import (
    ArticleEnricher,
    EnrichmentProviderError,
    EnrichmentRequest,
)
from backend.rag.application.use_cases.enrichment import (
    ENRICHMENT_VERSION,
    CancellationScope,
    EnrichmentSteps,
    bundled_retry_policy,
)
from backend.rag.application.use_cases.index import EnsureGeneration
from backend.rag.application.use_cases.ingest import IngestArticle
from backend.rag.domain.access import CallerScope
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.enrichment_draft import EnrichmentDraft
from backend.rag.domain.enrichment_lifecycle import (
    CancelAck,
    EnrichmentIdentity,
    FailureCode,
    JobOutcome,
    OutcomeKind,
    PublishResult,
)
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.llm.fake_enricher import (
    FakeArticleEnricher,
    ScriptedArticleEnricher,
)
from backend.rag.infra.postgres.connection import StorageOperationError
from backend.rag.infra.postgres.engine import rag_engine
from backend.rag.infra.postgres.migrations import migrate
from backend.rag.infra.postgres.publication import LockedHook
from backend.rag.infra.postgres.staging import PostgresStaging
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.repositories.postgres_enrichment import (
    PostgresEnrichmentRepository,
)
from backend.rag.infra.telemetry.telemetry import RecordingTelemetry
from backend.rag.workflows.ingestion.workflow import EnrichmentRunner
from backend.shared.contracts.article import Article
from enrichment_support import (
    CountingEmbeddings,
    document,
    grounded_draft,
)
from schema_support import prepare_schema

ROOT = Path(__file__).parents[4]
BUNDLE = ROOT / "fixtures/article_bundle.v1.json"
MIGRATIONS = ROOT / "src/backend/rag/infra/postgres/migrations"
CONTENT = "The Harbor Authority closed pier 4 after a storm. Ferries resume Monday."
CALLER = CallerScope(
    schema_version="caller_scope.v1",
    principal_id="fixture-analyst",
    scope_id="fixture-internal",
    policy_id="fixture-policy",
    policy_version="v1",
)
TIMEOUT = EnrichmentProviderError(FailureCode.PROVIDER_TIMEOUT)
POLICY = bundled_retry_policy()


@pytest.fixture
def url(database_url: str) -> Generator[str]:
    schema = "t4_" + uuid4().hex
    with psycopg.connect(database_url, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    scoped = make_conninfo(database_url, options=f"-c search_path={schema},public")
    prepare_schema(scoped)
    yield scoped
    with psycopg.connect(database_url, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )


def rows(
    url: str, query: str, params: Sequence[object] = ()
) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as connection:
        return list(connection.execute(query.encode(), params).fetchall())


def one(url: str, query: str, params: Sequence[object] = ()) -> object:
    (row,) = rows(url, query, params)
    return row[0]


def lexical(url: str, word: str) -> list[UUID]:
    return TypeAdapter(list[UUID]).validate_python(
        [
            row[0]
            for row in rows(
                url,
                """SELECT article_id FROM rag_current_lexical
                WHERE search_vector @@ plainto_tsquery('simple', %s)""",
                (word,),
            )
        ]
    )


def job_state(url: str, job_id: UUID) -> tuple[object, ...]:
    (row,) = rows(
        url,
        """SELECT status, attempt_count, claim_generation, failure_code
        FROM rag_enrichment_jobs WHERE job_id = %s""",
        (job_id,),
    )
    return row


def build(
    url: str,
    enricher: ArticleEnricher,
    embeddings: CountingEmbeddings | None = None,
    hook: LockedHook | None = None,
) -> tuple[EnrichmentRunner, PostgresEnrichmentRepository, EnrichmentSteps]:
    repository = PostgresEnrichmentRepository(rag_engine(url), publish_hook=hook)
    steps = EnrichmentSteps(
        repository=repository,
        enricher=enricher,
        generations=EnsureGeneration(
            PostgresStaging(rag_engine(url)),
            embeddings or CountingEmbeddings(),
            EmbeddingIdentity(),
        ),
        policy=POLICY,
        clock=lambda: datetime.now(UTC),
    )
    return EnrichmentRunner(steps, RecordingTelemetry()), repository, steps


def identity(enricher: ArticleEnricher) -> EnrichmentIdentity:
    return EnrichmentIdentity(
        ENRICHMENT_VERSION, enricher.model_version, enricher.prompt_version
    )


def ingest(url: str, content: str = CONTENT, article_id: UUID | None = None) -> UUID:
    article = document(content, article_id)
    key = identity(FakeArticleEnricher()).key_for(
        article.article_id, article.content_hash
    )
    receipt = PostgresEnrichmentRepository(rag_engine(url)).ingest(article, key)
    assert receipt.job_id is not None
    return receipt.job_id


def expire(url: str, job_id: UUID) -> None:
    with psycopg.connect(url) as connection:
        _ = connection.execute(
            """UPDATE rag_enrichment_jobs
            SET lease_expires_at = clock_timestamp() - interval '1 second'
            WHERE job_id = %s""",
            (job_id,),
        )


def make_eligible(url: str, job_id: UUID) -> None:
    """Simulate the DB clock passing next_attempt_at (no sleeping)."""
    with psycopg.connect(url) as connection:
        _ = connection.execute(
            """UPDATE rag_enrichment_jobs
            SET next_attempt_at = clock_timestamp() - interval '1 second'
            WHERE job_id = %s""",
            (job_id,),
        )


def fail_inserts(url: str, table: str) -> Callable[[], None]:
    name = sql.Identifier(f"t4_fail_{table}")
    with psycopg.connect(url, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL(
                """CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'injected interruption'; END $$"""
            ).format(name=name)
        )
        _ = connection.execute(
            sql.SQL(
                """CREATE TRIGGER {name} BEFORE INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION {name}()"""
            ).format(name=name, table=sql.Identifier(table))
        )

    def remove() -> None:
        with psycopg.connect(url, autocommit=True) as connection:
            _ = connection.execute(
                sql.SQL("DROP TRIGGER {name} ON {table}").format(
                    name=name, table=sql.Identifier(table)
                )
            )

    return remove


def upstream_article() -> Article:
    source = TypeAdapter(dict[str, object]).validate_json(BUNDLE.read_bytes())
    return Article.model_validate_json(TypeAdapter(object).dump_json(source["article"]))


def test_end_to_end_ingest_lexical_enrich_publish(url: str) -> None:
    article = upstream_article()
    receipt = IngestArticle(
        PostgresEnrichmentRepository(rag_engine(url)),
        FixtureAccessPolicy(),
        identity(FakeArticleEnricher()),
        lambda: datetime.now(UTC),
    ).execute(article, CALLER)
    assert receipt.job_id is not None
    # Lexical visibility before any enrichment work.
    assert lexical(url, "công") == [article.article_id]
    assert job_state(url, receipt.job_id) == ("PENDING", 0, 0, None)
    assert one(url, "SELECT count(*) FROM rag_current_vectors") == 0
    embeddings = CountingEmbeddings()
    runner, _, _ = build(url, FakeArticleEnricher(), embeddings)
    report = runner.run(5)
    assert [o.kind for o in report.outcomes] == [OutcomeKind.READY]
    assert job_state(url, receipt.job_id) == ("READY", 1, 1, None)
    generation = one(url, "SELECT active_index_generation FROM rag_article_current")
    assert report.outcomes[0].index_generation == generation
    assert one(url, "SELECT count(*) FROM rag_current_vectors") == embeddings.calls
    assert (
        one(url, "SELECT metadata->>'status' FROM rag_enrichment_versions") == "READY"
    )
    assert one(url, "SELECT count(*) FROM rag_enrichment_publications") == 1
    assert lexical(url, "công") == [article.article_id]


def test_duplicate_delivery_creates_no_duplicate_job_or_publication(url: str) -> None:
    first = ingest(url)
    article_id = TypeAdapter(UUID).validate_python(
        one(url, "SELECT article_id FROM rag_enrichment_jobs")
    )
    assert ingest(url, article_id=article_id) == first
    runner, _, _ = build(url, FakeArticleEnricher())
    assert len(runner.run(5).outcomes) == 1
    assert ingest(url, article_id=article_id) == first
    assert runner.run(5).outcomes == ()
    assert one(url, "SELECT count(*) FROM rag_enrichment_jobs") == 1
    assert one(url, "SELECT count(*) FROM rag_enrichment_publications") == 1


def test_concurrent_claims_have_exactly_one_valid_owner(url: str) -> None:
    job = ingest(url)
    _, repository, _ = build(url, FakeArticleEnricher())
    workers = 8
    barrier = Barrier(workers)
    ident = identity(FakeArticleEnricher())

    def claim(_: int) -> object:
        _ = barrier.wait()
        return repository.claim_next(ident, POLICY)

    with ThreadPoolExecutor(workers) as pool:
        claims = [c for c in pool.map(claim, range(workers)) if c is not None]
    assert len(claims) == 1
    assert job_state(url, job) == ("PROCESSING", 1, 1, None)


def _blocking_enricher(
    entered: Event, release: Event, before: Callable[[], None] | None = None
) -> ScriptedArticleEnricher:
    def step(request: EnrichmentRequest) -> EnrichmentDraft:
        if before is not None:
            before()
        entered.set()
        assert release.wait(timeout=30)
        return grounded_draft(request.context.normalized_content)

    return ScriptedArticleEnricher([step])


def test_expired_owner_a_cannot_write_after_b_reclaims_and_publishes(url: str) -> None:
    job = ingest(url)
    entered, release = Event(), Event()
    runner_a, _, _ = build(url, _blocking_enricher(entered, release))
    outcome_a: list[JobOutcome] = []
    thread = Thread(target=lambda: outcome_a.extend(runner_a.run(1).outcomes))
    thread.start()
    assert entered.wait(timeout=30)
    expire(url, job)
    runner_b, _, _ = build(url, FakeArticleEnricher())
    (outcome_b,) = runner_b.run(1).outcomes
    release.set()
    thread.join(timeout=30)
    assert outcome_b.kind is OutcomeKind.READY
    assert (outcome_b.claim_generation, outcome_b.attempt) == (2, 2)
    assert [o.kind for o in outcome_a] == [OutcomeKind.LOST_OWNERSHIP]
    assert job_state(url, job) == ("READY", 2, 2, None)
    assert rows(url, "SELECT claim_generation FROM rag_enrichment_publications") == [
        (2,)
    ]


def test_lease_expiry_without_reclaim_rejects_late_owner(url: str) -> None:
    job = ingest(url)
    entered, release = Event(), Event()
    runner, _, _ = build(
        url, _blocking_enricher(entered, release, before=lambda: expire(url, job))
    )
    release.set()
    (outcome,) = runner.run(1).outcomes
    assert outcome.kind is OutcomeKind.LOST_OWNERSHIP
    assert job_state(url, job)[0] == "PROCESSING"
    assert one(url, "SELECT count(*) FROM rag_enrichment_versions") == 0
    assert one(url, "SELECT active_index_generation FROM rag_article_current") is None
    recovered, _, _ = build(url, FakeArticleEnricher())
    (again,) = recovered.run(1).outcomes
    assert (again.kind, again.attempt) == (OutcomeKind.READY, 2)


def test_stale_fence_rejected_for_every_ownership_write(url: str) -> None:
    job = ingest(url)
    _, repository, steps = build(url, FakeArticleEnricher())
    claim = steps.claim()
    assert claim is not None
    stale = type(claim)(
        claim.job_id, claim.claim_generation + 1, claim.attempt, claim.key
    )
    assert not repository.schedule_retry(stale, FailureCode.PROVIDER_TIMEOUT, 30)
    assert not repository.mark_failed(stale, FailureCode.MALFORMED_OUTPUT)
    assert not repository.mark_stale(stale)
    assert repository.load_current(stale) is None
    assert repository.request_cancel(stale) is CancelAck.OWNERSHIP_LOST
    article = repository.load_current(claim)
    assert article is not None
    staged = steps.generations.execute(article)
    metadata = steps.validate(
        claim, article, grounded_draft(article.normalized_content), CancellationScope()
    )
    assert repository.publish(stale, staged.generation, metadata) is (  # pyright: ignore[reportArgumentType]
        PublishResult.LOST_OWNERSHIP
    )
    assert job_state(url, job) == ("PROCESSING", 1, 1, None)
    assert repository.publish(claim, staged.generation, metadata) is (  # pyright: ignore[reportArgumentType]
        PublishResult.PUBLISHED
    )


@pytest.mark.parametrize("change", ["correction", "tombstone", "supersession"])
def test_change_while_processing_blocks_publication(url: str, change: str) -> None:
    job = ingest(url)
    article_id = TypeAdapter(UUID).validate_python(
        one(url, "SELECT article_id FROM rag_enrichment_jobs")
    )

    def mutate(request: EnrichmentRequest) -> EnrichmentDraft:
        store = PostgresRagStore(rag_engine(url))
        if change == "correction":
            store.persist_article(document(CONTENT + " Pier 5 too.", article_id))
        elif change == "tombstone":
            store.tombstone_article(article_id)
        else:
            newer = identity(FakeArticleEnricher(prompt_version="prompt-new"))
            original = document(CONTENT, article_id)
            _ = PostgresEnrichmentRepository(rag_engine(url)).ingest(
                original, newer.key_for(article_id, original.content_hash)
            )
        return grounded_draft(request.context.normalized_content)

    runner, _, _ = build(url, ScriptedArticleEnricher([mutate]))
    (outcome,) = runner.run(1).outcomes
    assert outcome.kind is OutcomeKind.LOST_OWNERSHIP
    assert one(url, "SELECT count(*) FROM rag_enrichment_versions") == 0
    assert one(url, "SELECT active_index_generation FROM rag_article_current") is None
    _ = build(url, FakeArticleEnricher())[2].claim()
    assert job_state(url, job)[0] == "STALE"
    if change == "correction":
        assert lexical(url, "pier") == [article_id]
    if change == "tombstone":
        assert lexical(url, "pier") == []


def test_model_or_prompt_change_is_new_identity_and_reuses_embeddings(url: str) -> None:
    first = ingest(url)
    embeddings = CountingEmbeddings()
    _ = build(url, FakeArticleEnricher(), embeddings)[0].run(1)
    calls = embeddings.calls
    generation = one(url, "SELECT active_index_generation FROM rag_article_current")
    for revised in (
        FakeArticleEnricher(prompt_version="enrichment-prompt.v2"),
        FakeArticleEnricher(model_version="fake-enricher.v2"),
    ):
        (outcome,) = build(url, revised, embeddings)[0].run(1).outcomes
        assert outcome.kind is OutcomeKind.READY
        assert outcome.embedding_reused is True
        assert outcome.index_generation == generation
    assert embeddings.calls == calls
    assert one(url, "SELECT count(*) FROM rag_index_generations") == 1
    assert rows(
        url,
        "SELECT status FROM rag_enrichment_jobs ORDER BY created_at",
    ) == [("STALE",), ("STALE",), ("READY",)]
    assert job_state(url, first)[0] == "STALE"
    assert one(url, "SELECT count(*) FROM rag_enrichment_publications") == 3
    assert (
        one(
            url,
            """SELECT count(DISTINCT prompt_version || model_version)
            FROM rag_enrichment_publications""",
        )
        == 3
    )


def test_retry_schedule_on_db_clock_survives_restart_then_exhausts(url: str) -> None:
    job = ingest(url)
    enricher = ScriptedArticleEnricher([TIMEOUT, TIMEOUT, TIMEOUT])
    (first,) = build(url, enricher)[0].run(5).outcomes
    assert (first.kind, first.retry_in_seconds) == (OutcomeKind.RETRY_SCHEDULED, 30)
    assert job_state(url, job) == ("PENDING", 1, 1, "provider_timeout")
    delay = one(
        url,
        """SELECT extract(epoch FROM next_attempt_at - updated_at)::int
        FROM rag_enrichment_jobs""",
    )
    assert delay == 30
    # "Restart": fresh repository/runner objects read the schedule from the DB.
    assert build(url, enricher)[0].run(5).outcomes == ()
    make_eligible(url, job)
    (second,) = build(url, enricher)[0].run(5).outcomes
    assert (second.attempt, second.retry_in_seconds) == (2, 120)
    make_eligible(url, job)
    (third,) = build(url, enricher)[0].run(5).outcomes
    assert (third.kind, third.attempt) == (OutcomeKind.FAILED, 3)
    make_eligible(url, job)
    assert build(url, enricher)[0].run(5).outcomes == ()
    assert job_state(url, job) == ("FAILED", 3, 3, "provider_timeout")
    assert len(enricher.requests) == 3
    assert lexical(url, "harbor") != []


def test_validation_failure_is_failed_without_retry_and_lexical_survives(
    url: str,
) -> None:
    job = ingest(url)
    bad = grounded_draft(CONTENT).model_copy(update={"topics": ("sports",)})
    (outcome,) = build(url, ScriptedArticleEnricher([bad]))[0].run(5).outcomes
    assert (outcome.kind, outcome.failure_code) == (
        OutcomeKind.FAILED,
        FailureCode.UNSUPPORTED_TAXONOMY,
    )
    make_eligible(url, job)
    assert build(url, FakeArticleEnricher())[0].run(5).outcomes == ()
    assert job_state(url, job) == ("FAILED", 1, 1, "unsupported_taxonomy")
    assert len(lexical(url, "harbor")) == 1
    assert one(url, "SELECT count(*) FROM rag_index_generations") == 0


def test_crash_inside_ingest_rolls_back_both_halves_then_retry_succeeds(
    url: str,
) -> None:
    remove = fail_inserts(url, "rag_enrichment_jobs")
    with pytest.raises(StorageOperationError) as failed:
        _ = ingest(url)
    assert isinstance(failed.value.__cause__, RaiseException)
    assert one(url, "SELECT count(*) FROM rag_article_documents") == 0
    remove()
    _ = ingest(url)
    assert one(url, "SELECT count(*) FROM rag_enrichment_jobs") == 1


def test_article_persisted_without_job_is_reconciled_after_restart(url: str) -> None:
    PostgresRagStore(rag_engine(url)).persist_article(document(CONTENT))
    assert one(url, "SELECT count(*) FROM rag_enrichment_jobs") == 0
    report = build(url, FakeArticleEnricher())[0].run(5)
    assert report.reconciled == 1
    assert [o.kind for o in report.outcomes] == [OutcomeKind.READY]


@pytest.mark.parametrize("table", ["rag_chunks", "rag_enrichment_publications"])
def test_interrupted_stage_or_publish_rolls_back_and_retry_is_consistent(
    url: str, table: str
) -> None:
    job = ingest(url)
    remove = fail_inserts(url, table)
    with pytest.raises(StorageOperationError) as failed:
        _ = build(url, FakeArticleEnricher())[0].run(1)
    assert isinstance(failed.value.__cause__, RaiseException)
    remove()
    assert job_state(url, job)[0] == "PROCESSING"
    assert one(url, "SELECT count(*) FROM rag_enrichment_versions") == 0
    assert one(url, "SELECT active_index_generation FROM rag_article_current") is None
    assert one(url, "SELECT count(*) FROM rag_current_vectors") == 0
    if table == "rag_chunks":
        assert one(url, "SELECT count(*) FROM rag_index_generations") == 0
    expire(url, job)
    (outcome,) = build(url, FakeArticleEnricher())[0].run(1).outcomes
    assert outcome.kind is OutcomeKind.READY
    assert one(url, "SELECT count(*) FROM rag_enrichment_versions") == 1
    assert one(url, "SELECT count(*) FROM rag_index_generations") == 1
    assert one(url, "SELECT active_index_generation FROM rag_article_current") == (
        outcome.index_generation
    )


def test_cancel_acknowledged_before_publication_starts_no_more_work(url: str) -> None:
    job = ingest(url)
    _ = ingest(url, CONTENT + " A second article.")
    scope = CancellationScope()
    embeddings = CountingEmbeddings()
    acks: list[CancelAck | None] = []

    def cancel(request: EnrichmentRequest) -> EnrichmentDraft:
        acks.append(scope.cancel())
        return grounded_draft(request.context.normalized_content)

    enricher = ScriptedArticleEnricher([cancel])
    report = build(url, enricher, embeddings)[0].run(5, scope)
    assert acks == [CancelAck.ACKNOWLEDGED]
    assert [o.kind for o in report.outcomes] == [OutcomeKind.CANCELLED]
    assert len(enricher.requests) == 1
    assert embeddings.calls == 0
    assert one(url, "SELECT count(*) FROM rag_index_generations") == 0
    cancelled = report.outcomes[0].job_id
    state = rows(
        url,
        """SELECT status, failure_code, cancel_requested_generation = claim_generation
        FROM rag_enrichment_jobs WHERE job_id = %s""",
        (cancelled,),
    )
    assert state == [("PROCESSING", None, True)]
    assert {job, cancelled} >= {cancelled}
    expire(url, cancelled)
    recovered = build(url, FakeArticleEnricher())[0].run(5)
    assert {o.kind for o in recovered.outcomes} == {OutcomeKind.READY}
    assert job_state(url, cancelled)[0] == "READY"


def test_cancel_committed_before_publish_transaction_wins(url: str) -> None:
    _ = ingest(url)
    _, repository, steps = build(url, FakeArticleEnricher())
    claim = steps.claim()
    assert claim is not None
    article = repository.load_current(claim)
    assert article is not None
    staged = steps.generations.execute(article)
    metadata = steps.validate(
        claim, article, grounded_draft(article.normalized_content), CancellationScope()
    )
    assert repository.request_cancel(claim) is CancelAck.ACKNOWLEDGED
    assert repository.publish(claim, staged.generation, metadata) is (  # pyright: ignore[reportArgumentType]
        PublishResult.CANCELLED
    )
    assert one(url, "SELECT count(*) FROM rag_enrichment_versions") == 0
    assert one(url, "SELECT active_index_generation FROM rag_article_current") is None


def _wait_for_lock_waiter(url: str) -> None:
    """Block until another backend waits on a row lock (observation, no sleep)."""
    with psycopg.connect(url, autocommit=True) as connection:
        for _ in range(100_000):
            waiting = connection.execute(
                """SELECT count(*) FROM pg_stat_activity
                WHERE wait_event_type = 'Lock' AND datname = current_database()"""
            ).fetchone()
            if waiting is not None and waiting[0] == 1:
                return
    pytest.fail("cancel request never blocked on the publication row lock")


def test_publication_committed_first_survives_concurrent_cancel(url: str) -> None:
    job = ingest(url)
    scope = CancellationScope()
    acks: list[CancelAck | None] = []
    cancel_thread: list[Thread] = []

    def while_locked() -> None:
        thread = Thread(target=lambda: acks.append(scope.cancel()))
        cancel_thread.append(thread)
        thread.start()
        _wait_for_lock_waiter(url)

    runner, _, _ = build(url, FakeArticleEnricher(), hook=while_locked)
    report = runner.run(1, scope)
    cancel_thread[0].join(timeout=30)
    assert [o.kind for o in report.outcomes] == [OutcomeKind.READY]
    assert acks == [CancelAck.ALREADY_PUBLISHED]
    assert job_state(url, job)[0] == "READY"
    assert one(url, "SELECT count(*) FROM rag_enrichment_publications") == 1


def test_upgrade_from_g1_schema_keeps_rows_and_adds_lifecycle(
    database_url: str,
) -> None:
    schema = "t4_upgrade_" + uuid4().hex
    with psycopg.connect(database_url, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    scoped = make_conninfo(database_url, options=f"-c search_path={schema},public")
    try:
        legacy = sorted(MIGRATIONS.glob("00[1-5]_*.sql"))
        assert len(legacy) == 5
        with psycopg.connect(scoped) as connection:
            for path in legacy:
                _ = connection.execute(path.read_bytes())
        # G1-era ledger + data written by the T3 primitives.
        with psycopg.connect(scoped) as connection:
            _ = connection.execute(
                """CREATE TABLE rag_schema_migrations (filename text PRIMARY KEY,
                checksum text NOT NULL, applied_at timestamptz NOT NULL
                DEFAULT clock_timestamp())"""
            )
            for path in legacy:
                _ = connection.execute(
                    "INSERT INTO rag_schema_migrations VALUES (%s, %s, DEFAULT)",
                    (path.name, sha256(path.read_bytes()).hexdigest()),
                )
        store = PostgresRagStore(rag_engine(scoped))
        article = document(CONTENT)
        store.persist_article(article)
        job = store.enqueue_job(
            article.article_id, article.content_hash, "v1", "v1", "v1"
        )
        migrate(scoped)
        assert job_state(scoped, job) == ("PENDING", 0, 0, None)
        assert one(scoped, "SELECT count(*) FROM rag_schema_migrations") == 7
        assert one(scoped, "SELECT count(*) FROM rag_enrichment_publications") == 0
        assert lexical(scoped, "harbor") == [article.article_id]
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            _ = connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def test_publication_history_is_append_only(url: str) -> None:
    _ = ingest(url)
    _ = build(url, FakeArticleEnricher())[0].run(1)
    with pytest.raises(RaiseException), psycopg.connect(url) as connection:
        _ = connection.execute("DELETE FROM rag_enrichment_publications")
