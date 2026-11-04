"""``rag_0004_news_backend`` upgrade/downgrade over a populated 0003 schema."""

from collections.abc import Generator
from hashlib import sha256
from typing import cast
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from psycopg import sql
from psycopg.conninfo import make_conninfo

from backend.rag.composition import FIXTURE_CALLER, FIXTURES
from backend.rag.infra.postgres.alembic_runner import (
    adopt_legacy,
    alembic_config,
    upgrade_head,
)
from backend.rag.infra.postgres.engine import rag_engine, transaction
from backend.rag.infra.postgres.migrations import migrate
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.repositories.postgres_source_references import (
    PostgresSourceReferences,
)
from backend.rag.infra.retrieval.memory_retrieval import load_fixture_articles
from backend.shared.contracts.evidence import EvidenceSpan
from schema_support import MIGRATIONS, schema_path

KEPT = (
    "rag_article_documents",
    "rag_article_current",
    "rag_source_references",
    "rag_enrichment_jobs",
    "rag_search_outbox",
)
PENDING_SUMMARIES = """SELECT count(*), count(DISTINCT subject_id) FROM rag_work_jobs
WHERE kind = 'summary' AND status = 'PENDING'"""
SEEDED = """SELECT (SELECT count(*) FROM rag_topics),
    (SELECT count(*) FROM rag_locations WHERE location_type <> 'former_province')"""
NEW = (
    "rag_topics",
    "rag_locations",
    "rag_work_jobs",
    "rag_conversations",
    "rag_query_runs",
)


@pytest.fixture
def schema_0003(database_url: str) -> Generator[str]:
    """Empty schema migrated (fresh or adopted path) only up to rag_0003."""
    schema = "mig_" + uuid4().hex
    with psycopg.connect(database_url, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    url = make_conninfo(database_url, options=f"-c search_path={schema},public")
    if schema_path() == "adopted":
        migrate(url, MIGRATIONS)
        _ = adopt_legacy(url)
    _move(url, "upgrade", "rag_0003_search_platform")
    yield url
    with psycopg.connect(database_url, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )


def _move(url: str, direction: str, revision: str) -> None:
    with transaction(rag_engine(url)) as connection:
        step = command.upgrade if direction == "upgrade" else command.downgrade
        step(alembic_config(connection), revision)


def _counts(url: str, tables: tuple[str, ...]) -> dict[str, int]:
    found: dict[str, int] = {}
    with psycopg.connect(url) as connection:
        for table in tables:
            row = connection.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()
            found[table] = cast("int", row[0]) if row else -1
    return found


def _exists(url: str, table: str) -> bool:
    with psycopg.connect(url) as connection:
        row = connection.execute(
            """SELECT EXISTS (SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relname = %s)""",
            (table,),
        ).fetchone()
    return bool(row and row[0])


def test_upgrade_keeps_data_backfills_jobs_and_downgrade_is_additive(
    schema_0003: str,
) -> None:
    store = PostgresRagStore(rag_engine(schema_0003))
    documents = load_fixture_articles(FIXTURES)
    for document in documents:
        store.persist_article(document)
    store.tombstone_article(documents[0].article_id)
    content = documents[1].normalized_content
    excerpt = content[:20]
    _ = PostgresSourceReferences(rag_engine(schema_0003)).issue(
        FIXTURE_CALLER,
        EvidenceSpan(
            span_id=uuid4(),
            article_id=documents[1].article_id,
            content_hash=documents[1].content_hash,
            start_offset=0,
            end_offset=20,
            excerpt=excerpt,
            span_fingerprint=f"sha256:{sha256(excerpt.encode()).hexdigest()}",
            offset_basis="unicode_codepoint",
        ),
    )
    before = _counts(schema_0003, KEPT)

    assert upgrade_head(schema_0003) == "rag_0004_news_backend"
    after = _counts(schema_0003, KEPT)
    with psycopg.connect(schema_0003) as connection:
        jobs = connection.execute(PENDING_SUMMARIES).fetchone()
        seeded = connection.execute(SEEDED).fetchone()

    assert after == before
    assert jobs == (len(documents) - 1, len(documents) - 1)  # tombstone: no job
    assert seeded == (11, 35)  # 11 topics; Việt Nam + 34 province-level units

    _move(schema_0003, "downgrade", "rag_0003_search_platform")
    assert _counts(schema_0003, KEPT) == before
    assert not any(_exists(schema_0003, table) for table in NEW)
    assert upgrade_head(schema_0003) == "rag_0004_news_backend"  # re-upgradable


def test_new_revision_trigger_schedules_exactly_one_summary_job(
    schema_0003: str,
) -> None:
    _ = upgrade_head(schema_0003)
    store = PostgresRagStore(rag_engine(schema_0003))
    document = load_fixture_articles(FIXTURES)[2]
    store.persist_article(document)
    store.persist_article(document)  # duplicate delivery
    with psycopg.connect(schema_0003) as connection:
        rows = connection.execute(
            "SELECT revision_key, status FROM rag_work_jobs WHERE subject_id = %s",
            (document.article_id,),
        ).fetchall()
    assert rows == [(document.content_hash, "PENDING")]
