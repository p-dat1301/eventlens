"""T3 local database maintenance CLI."""

import subprocess
import sys
from pathlib import Path
from typing import Annotated

import psycopg
import typer
from psycopg.rows import scalar_row
from pydantic import TypeAdapter

from eventlens_rag.adapters.fake_embeddings import FakeEmbeddings
from eventlens_rag.adapters.postgres import PostgresRagStore
from eventlens_rag.adapters.staging import PostgresStaging
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.chunking import chunk_document
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.domain.staging import StageRequest

app = typer.Typer(no_args_is_help=True)
ROOT = Path(__file__).parents[4]
MIGRATIONS = ROOT / "apps/rag/migrations"


def _store(database_url: str) -> PostgresRagStore:
    """Create explicit local PostgreSQL adapter."""
    return PostgresRagStore(database_url)


@app.command()
def migrate(
    database_url: Annotated[str, typer.Option(envvar="EVENTLENS_RAG_DATABASE_URL")],
) -> None:
    """Apply RAG PostgreSQL and pgvector migrations."""
    _store(database_url).migrate(MIGRATIONS)
    typer.echo("rag migrations applied")


@app.command()
def rebuild(
    database_url: Annotated[str, typer.Option(envvar="EVENTLENS_RAG_DATABASE_URL")],
) -> None:
    """Rebuild current persisted articles with deterministic fake embeddings."""
    _store(database_url).migrate(MIGRATIONS)
    with psycopg.connect(database_url) as connection:
        rows = TypeAdapter(list[str]).validate_python(
            connection.cursor(row_factory=scalar_row)
            .execute("SELECT document_json::text FROM rag_current_lexical")
            .fetchall()
        )
    staging = PostgresStaging(database_url)
    embeddings = FakeEmbeddings()
    published = 0
    for row in rows:
        article = ArticleDocument.model_validate_json(
            TypeAdapter(str).validate_python(row)
        )
        chunks = chunk_document(article)
        request = StageRequest(
            article,
            EmbeddingIdentity(),
            chunks,
            tuple(embeddings.embed(chunk.content) for chunk in chunks),
        )
        generation = staging.stage(request)
        published += int(staging.activate(generation))
    typer.echo(f"rag fake rebuild published {published} current articles")


@app.command("db-test")
def db_test() -> None:
    """Run isolated real PostgreSQL suite using an owned ephemeral container."""
    completed = subprocess.run(
        ["/bin/sh", "apps/rag/db-test.sh"],
        cwd=ROOT,
        check=False,
    )
    sys.exit(completed.returncode)
