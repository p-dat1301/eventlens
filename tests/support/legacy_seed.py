"""Seed a RAG schema with every kind of row, and fingerprint its contents.

The seed goes through the production adapters only (no migration calls), so
it works on a historical-runner schema before adoption and on an Alembic one.
It leaves: 30 fixture revisions plus one corrected revision, READY and FAILED
jobs, a STALE job, a PENDING job, complete generations with chunks, fenced
publications, a tombstoned article and two source references.
"""

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg import sql
from pydantic import TypeAdapter

from backend.rag.composition import FIXTURE_CALLER, build_runtime
from backend.rag.domain.article_document import (
    ArticleDocument,
    normalized_content_hash,
)
from backend.rag.infra.diagnostics.retrieval_smoke import enrich_fixture_corpus
from backend.rag.infra.postgres.engine import rag_engine
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.repositories.postgres_source_references import (
    PostgresSourceReferences,
)
from backend.rag.infra.retrieval.memory_retrieval import load_fixture_articles
from backend.shared.contracts.article import Article

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "fixtures/rag"
BUNDLE = ROOT / "fixtures/article_bundle.v1.json"
TABLES = (
    "rag_article_documents",
    "rag_article_current",
    "rag_enrichment_jobs",
    "rag_index_generations",
    "rag_chunks",
    "rag_enrichment_versions",
    "rag_enrichment_publications",
    "rag_source_references",
    "rag_schema_migrations",
)


@dataclass(frozen=True, slots=True)
class Seeded:
    """Identifiers a test needs after seeding."""

    corrected: ArticleDocument
    tombstoned: UUID
    tokens: tuple[str, ...]


def _corrected(document: ArticleDocument) -> ArticleDocument:
    content = document.normalized_content + " Correction appended."
    digest = normalized_content_hash(content)
    return document.model_copy(
        update={
            "raw_content": content,
            "normalized_content": content,
            "content_hash": digest,
            "content_fingerprint": digest,
            "evidence_spans": (),
        }
    )


def seed(url: str) -> Seeded:
    """Fill ``url`` (an already migrated schema) through production adapters."""
    engine = rag_engine(url)
    store = PostgresRagStore(engine)
    documents = list(load_fixture_articles(FIXTURES))
    for document in documents:
        store.persist_article(document)
    enrich_fixture_corpus(url, FIXTURES, documents)
    corrected = _corrected(documents[0])
    store.persist_article(corrected)
    store.tombstone_article(documents[1].article_id)
    article = Article.model_validate_json(_bundle_article())
    _ = build_runtime(url).ingest.execute(article, FIXTURE_CALLER)
    references = PostgresSourceReferences(engine)
    tokens = tuple(
        references.issue(FIXTURE_CALLER, document.evidence_spans[0])
        for document in documents[2:4]
    )
    return Seeded(corrected, documents[1].article_id, tokens)


def _bundle_article() -> str:
    bundle = TypeAdapter(dict[str, object]).validate_json(BUNDLE.read_bytes())
    return json.dumps(bundle["article"])


def fingerprint(url: str) -> dict[str, tuple[int, str]]:
    """Row count and SHA-256 of every row (all columns, PK order) per table."""
    result: dict[str, tuple[int, str]] = {}
    with psycopg.connect(url) as connection:
        for table in TABLES:
            query = sql.SQL("SELECT t::text FROM {} t ORDER BY 1").format(
                sql.Identifier(table)
            )
            rows = TypeAdapter(list[str]).validate_python(
                [row[0] for row in connection.execute(query).fetchall()]
            )
            digest = sha256("\n".join(rows).encode()).hexdigest()
            result[table] = (len(rows), digest)
    return result
