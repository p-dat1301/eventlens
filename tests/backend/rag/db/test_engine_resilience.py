"""SQLAlchemy Engine behavior on real PostgreSQL: pooling, outages, typed errors.

The pre-SQLAlchemy adapters opened one connection per operation; the pooled
Engine must keep the same guarantees: a cut mid-transaction rolls the whole
operation back, the pool recovers after the server drops its connections,
concurrent workers sharing one pool still publish each job exactly once, and
no error surface carries SQL, bound values or the connection string.
"""

import os
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import TypeAdapter

from backend.rag.application.use_cases.enrichment import (
    ENRICHMENT_VERSION,
    EnrichmentSteps,
    bundled_retry_policy,
)
from backend.rag.application.use_cases.index import EnsureGeneration
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.enrichment_lifecycle import EnrichmentIdentity, OutcomeKind
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings
from backend.rag.infra.llm.fake_enricher import FakeArticleEnricher
from backend.rag.infra.postgres.connection import (
    StorageOperationError,
    StorageUnavailableError,
)
from backend.rag.infra.postgres.engine import (
    create_rag_engine,
    execute,
    rag_engine,
    transaction,
)
from backend.rag.infra.postgres.publication import LockedHook
from backend.rag.infra.postgres.staging import PostgresStaging
from backend.rag.infra.repositories.postgres_enrichment import (
    PostgresEnrichmentRepository,
)
from backend.rag.infra.telemetry.telemetry import RecordingTelemetry
from backend.rag.workflows.ingestion.workflow import EnrichmentRunner, RunReport
from schema_support import prepare_schema
from tcp_proxy import TcpProxy

type Factory = Callable[[str], ArticleDocument]
ENRICHER = FakeArticleEnricher()
IDENTITY = EnrichmentIdentity(
    ENRICHMENT_VERSION, ENRICHER.model_version, ENRICHER.prompt_version
)


@pytest.fixture
def url(database_url: str) -> Generator[str]:
    schema = "engine_" + uuid4().hex
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


@pytest.fixture
def proxy(url: str) -> Generator[TcpProxy]:
    params = conninfo_to_dict(url)
    relay = TcpProxy(target_port=int(str(params.get("port", 5432))))
    relay.start()
    yield relay
    relay.stop()


def _via(url: str, relay: TcpProxy) -> str:
    return make_conninfo(url, host="127.0.0.1", port=relay.port)


def _count(url: str, query: str) -> int:
    with psycopg.connect(url) as connection:
        row = connection.execute(query.encode()).fetchone()
    assert row is not None
    return TypeAdapter(int).validate_python(row[0])


def _runner(url: str, hook: LockedHook | None = None) -> EnrichmentRunner:
    engine = rag_engine(url)
    steps = EnrichmentSteps(
        repository=PostgresEnrichmentRepository(engine, publish_hook=hook),
        enricher=ENRICHER,
        generations=EnsureGeneration(
            PostgresStaging(engine), FakeEmbeddings(), EmbeddingIdentity()
        ),
        policy=bundled_retry_policy(),
        clock=lambda: datetime.now(UTC),
    )
    return EnrichmentRunner(steps, RecordingTelemetry())


def _run_five(url: str) -> RunReport:
    return _runner(url).run(5)


def _ingest(url: str, document: ArticleDocument) -> None:
    receipt = PostgresEnrichmentRepository(rag_engine(url)).ingest(
        document, IDENTITY.key_for(document.article_id, document.content_hash)
    )
    assert receipt.job_id is not None


def test_cut_inside_fenced_publication_rolls_back_then_recovers(
    url: str, proxy: TcpProxy, make_article: Factory
) -> None:
    # Given: one job; the worker reaches PostgreSQL only through the proxy.
    _ingest(url, make_article("Harbor cranes resumed loading after the storm."))
    cut: list[bool] = []

    def cut_once() -> None:
        if not cut:
            cut.append(True)
            proxy.stop()

    # When: the connection dies while publication holds its row locks.
    with pytest.raises(StorageUnavailableError):
        _ = _runner(_via(url, proxy), cut_once).run(1)

    # Then: nothing of the publication transaction survived.
    assert _count(url, "SELECT count(*) FROM rag_enrichment_publications") == 0
    assert _count(url, "SELECT count(*) FROM rag_enrichment_versions") == 0
    assert _count(url, "SELECT count(*) FROM rag_current_vectors") == 0
    assert (
        _count(url, "SELECT count(*) FROM rag_enrichment_jobs WHERE status = 'READY'")
        == 0
    )

    # When: the database is reachable again and the lease has expired.
    proxy.start()
    with psycopg.connect(url) as connection:
        _ = connection.execute(
            """UPDATE rag_enrichment_jobs
            SET lease_expires_at = clock_timestamp() - interval '1 second'"""
        )
    report = _runner(_via(url, proxy)).run(1)

    # Then: the same pooled Engine recovers and publishes exactly once.
    assert [outcome.kind for outcome in report.outcomes] == [OutcomeKind.READY]
    assert _count(url, "SELECT count(*) FROM rag_enrichment_publications") == 1
    assert _count(url, "SELECT count(*) FROM rag_current_vectors") > 0


def test_pool_recovers_after_server_terminates_its_connections(url: str) -> None:
    # Given: a pooled Engine that already holds an idle connection.
    engine = rag_engine(url)
    with transaction(engine) as connection:
        pid = TypeAdapter(int).validate_python(
            execute(connection, "SELECT pg_backend_pid()").scalar()
        )

    # When: the server terminates that backend behind the pool's back.
    with psycopg.connect(url, autocommit=True) as admin:
        _ = admin.execute("SELECT pg_terminate_backend(%s)", (pid,))

    # Then: pre-ping replaces it; the next operation succeeds.
    with transaction(engine) as connection:
        assert execute(connection, "SELECT 1").scalar() == 1


def test_workers_sharing_one_pool_publish_each_job_once(
    url: str, make_article: Factory
) -> None:
    # Given: more jobs than pooled connections, four concurrent workers.
    for number in range(12):
        _ingest(url, make_article(f"Port bulletin {number}: berth {number} open."))

    # When
    with ThreadPoolExecutor(4) as pool:
        reports = list(pool.map(_run_five, [url] * 4))

    # Then: one READY publication per job, never two owners.
    ready = [
        outcome
        for report in reports
        for outcome in report.outcomes
        if outcome.kind is OutcomeKind.READY
    ]
    assert len(ready) == 12
    assert _count(url, "SELECT count(*) FROM rag_enrichment_publications") == 12
    assert (
        _count(
            url,
            """SELECT count(*) FROM (SELECT job_id FROM rag_enrichment_publications
            GROUP BY job_id HAVING count(*) > 1) duplicated""",
        )
        == 0
    )


def test_errors_never_carry_sql_bound_values_or_credentials() -> None:
    # Given: a DSN with a password, and a failing statement with a bound secret.
    base = os.environ.get("EVENTLENS_RAG_DATABASE_URL")
    if base is None:
        pytest.skip("EVENTLENS_RAG_DATABASE_URL required for real PostgreSQL test")
    engine = create_rag_engine(base)
    secret = "bound-secret-" + uuid4().hex
    password = str(conninfo_to_dict(base).get("password", ""))

    # When
    with pytest.raises(StorageOperationError) as failed, transaction(engine) as c:
        _ = execute(c, "SELECT %s::text, 1 / 0", (secret,))
    engine.dispose()
    closed = create_rag_engine("postgresql://u:closed-secret@127.0.0.1:1/none")
    with pytest.raises(StorageUnavailableError) as unavailable, transaction(closed):
        pass

    # Then: only generic messages; the SQLAlchemy error (with SQL) is not chained
    # for display, and the driver cause names no statement or value.
    for error in (failed.value, unavailable.value):
        assert error.__suppress_context__
        rendered = " ".join((str(error), repr(error), str(error.__cause__)))
        assert secret not in rendered
        assert "1 / 0" not in rendered
        assert "closed-secret" not in rendered
        assert not password or password not in rendered
    assert "closed-secret" not in repr(closed)
    assert failed.value.code == "rag_storage_failed"
    assert unavailable.value.code == "rag_storage_unavailable"
