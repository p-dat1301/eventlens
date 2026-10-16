from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import TypeAdapter
from typer.testing import CliRunner

from eventlens_rag.adapters.migrations import LegacySchemaError, migrate
from eventlens_rag.cli import app

# Shape of the untracked pre-ledger tables found in the local Compose `eventlens`
# database (read-only `pg_dump --schema-only`, 2026-10-16). No migration ledger.
LEGACY_COMPOSE_DDL = """
CREATE TABLE article_documents (
    article_id uuid NOT NULL, source_id uuid NOT NULL, title text NOT NULL,
    normalized_content text NOT NULL, canonical_url text NOT NULL,
    source_domain text NOT NULL, source_published_at timestamptz,
    content_hash text NOT NULL, visibility text NOT NULL DEFAULT 'internal',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, content_hash));
CREATE TABLE rag_chunks (
    chunk_id uuid PRIMARY KEY, article_id uuid NOT NULL, content_hash text NOT NULL,
    chunk_index integer NOT NULL, chunk_fingerprint text NOT NULL UNIQUE,
    content text NOT NULL, embedding vector(1536));
CREATE TABLE rag_document_versions (
    article_id uuid NOT NULL, content_hash text NOT NULL, version text NOT NULL,
    PRIMARY KEY (article_id, content_hash));
CREATE TABLE rag_index_metadata (
    index_name text PRIMARY KEY, index_version text NOT NULL,
    embedding_dimensions integer NOT NULL CHECK (embedding_dimensions = 1536));
CREATE TABLE rag_jobs (
    job_id uuid PRIMARY KEY, article_id uuid NOT NULL, content_hash text NOT NULL,
    idempotency_key text NOT NULL UNIQUE, status text NOT NULL);
"""


def _legacy_schema(database_url: str) -> tuple[str, str]:
    schema = "compose_legacy_" + uuid4().hex
    with psycopg.connect(database_url) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    scoped = make_conninfo(database_url, options=f"-c search_path={schema},public")
    with psycopg.connect(scoped) as connection:
        _ = connection.execute(LEGACY_COMPOSE_DDL.encode())
        _ = connection.execute(
            """INSERT INTO rag_index_metadata VALUES ('legacy', 'v0', 1536)"""
        )
    return schema, scoped


def _tables(scoped_url: str, schema: str) -> list[tuple[str, str]]:
    with psycopg.connect(scoped_url) as connection:
        rows = connection.execute(
            """SELECT table_name, column_name FROM information_schema.columns
            WHERE table_schema = %s ORDER BY table_name, ordinal_position""",
            (schema,),
        ).fetchall()
    return TypeAdapter(list[tuple[str, str]]).validate_python(rows)


def test_untracked_legacy_compose_schema_fails_closed_and_rolls_back(
    database_url: str,
) -> None:
    # Given: legacy RAG tables without a migration ledger, plus one legacy row.
    schema, scoped = _legacy_schema(database_url)
    before = _tables(scoped, schema)
    # When: migration meets a table it would create with a different shape.
    with pytest.raises(LegacySchemaError, match="rag_chunks"):
        migrate(scoped)
    # Then: no ledger or new tables were committed; legacy tables and data intact.
    assert _tables(scoped, schema) == before
    with psycopg.connect(scoped) as connection:
        ledger = connection.execute(
            "SELECT to_regclass(%s)", (f"{schema}.rag_schema_migrations",)
        ).fetchone()
        kept = connection.execute("SELECT index_name FROM rag_index_metadata")
        assert kept.fetchall() == [("legacy",)]
    assert ledger == (None,)


def test_cli_reports_typed_failure_for_legacy_compose_schema(
    database_url: str,
) -> None:
    # Given: the untracked legacy shape.
    schema, scoped = _legacy_schema(database_url)
    before = _tables(scoped, schema)
    # When: operator runs the CLI against it.
    result = CliRunner().invoke(app, ["migrate", "--database-url", scoped])
    # Then: typed, nonzero exit without a success message or schema change.
    assert result.exit_code == 1
    assert "rag_migration_failed" in result.output
    assert "rag_chunks" in result.output
    assert "applied" not in result.output
    assert _tables(scoped, schema) == before
