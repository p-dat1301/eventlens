"""Historical 001-007 SQL runner and its checksum-verified ledger.

Alembic is the RAG migration authority (``alembic.py``). This runner is kept
only to audit and reproduce the pre-Alembic history: it builds legacy-shaped
databases for adoption tests and verifies ``rag_schema_migrations``. It
refuses to apply any script to a database that Alembic already manages, so
the two tools never both change one schema.
"""

from hashlib import sha256
from importlib.resources import files
from pathlib import Path

from pydantic import TypeAdapter
from sqlalchemy import Connection

from backend.rag.infra.postgres.engine import (
    create_rag_engine,
    execute,
    relation_exists,
    scalar,
    scalars,
    transaction,
)

# Shared with Alembic's env.py so the two runners never interleave on one DB.
MIGRATION_LOCK_KEY = 734528109

EXPECTED_MIGRATIONS = (
    "001_enable_vector.sql",
    "002_create_rag_index.sql",
    "003_storage_guards.sql",
    "004_staged_publication.sql",
    "005_current_lexical_document.sql",
    "006_enrichment_lifecycle.sql",
    "007_source_references.sql",
)


class MigrationChangedError(RuntimeError):
    """Previously applied migration differs from its recorded checksum."""

    def __init__(self, filename: str) -> None:
        """Identify the historical migration requiring investigation."""
        super().__init__(f"migration checksum mismatch: {filename}")


class MigrationResourcesError(RuntimeError):
    """Migration scripts are missing or incomplete."""

    def __init__(self, directory: str) -> None:
        """Name the missing SQL location for operator recovery."""
        super().__init__(f"migration SQL resources missing or incomplete: {directory}")


class LegacyDocumentError(RuntimeError):
    """A legacy revision cannot be reconstructed from its stored columns."""

    def __init__(self) -> None:
        """Explain why restoring original documents is required."""
        super().__init__(
            """Legacy ArticleDocument document_json is NULL; restore exact original
ArticleDocuments from an approved source before migrating or rebuilding.
Existing title/content columns cannot reconstruct full documents."""
        )


class LegacySchemaError(RuntimeError):
    """Tables predate the migration ledger and differ from what it would create."""

    def __init__(self, tables: tuple[str, ...]) -> None:
        """Name conflicting tables; never drop or rewrite them automatically."""
        super().__init__(
            f"""Untracked legacy RAG tables exist without migration records:
{", ".join(tables)}. Nothing was changed. Use a separate database or schema for
the current RAG index; restore legacy content only from exact original
ArticleDocuments."""
        )


class AlembicManagedError(RuntimeError):
    """The legacy runner was pointed at a database Alembic manages."""

    def __init__(self, filename: str) -> None:
        """Name the script that would have been applied; nothing was changed."""
        super().__init__(
            f"""Refusing to apply {filename}: this database is managed by Alembic
(alembic_version exists). Use `eventlens-rag migrate`; the legacy runner only
audits the historical ledger."""
        )


# Tables created by 002; existing copies without a recorded 002 are foreign shapes.
LEDGER_OWNED_TABLES = (
    "rag_article_documents",
    "rag_article_current",
    "rag_enrichment_jobs",
    "rag_index_generations",
    "rag_chunks",
)


def bundled_migrations() -> tuple[tuple[str, str], ...]:
    """Load SQL from installed package, including wheel and zip imports."""
    directory = files("backend.rag.infra.postgres").joinpath("migrations")
    if not directory.is_dir() or any(
        not directory.joinpath(name).is_file() for name in EXPECTED_MIGRATIONS
    ):
        raise MigrationResourcesError(str(directory))
    return tuple(
        (name, directory.joinpath(name).read_text(encoding="utf-8"))
        for name in EXPECTED_MIGRATIONS
    )


def _guard_unrecorded(connection: Connection, filename: str) -> None:
    """Fail closed before an unrecorded script meets data it cannot upgrade."""
    if relation_exists(connection, "alembic_version"):
        raise AlembicManagedError(filename)
    if filename.startswith("002_"):
        conflicts = TypeAdapter(tuple[str, ...]).validate_python(
            scalars(
                execute(
                    connection,
                    """SELECT table_name::text FROM information_schema.tables
                WHERE table_schema = current_schema()
                AND table_name = ANY(%s) ORDER BY table_name""",
                    (list(LEDGER_OWNED_TABLES),),
                )
            )
        )
        if conflicts:
            raise LegacySchemaError(conflicts)
    if filename.startswith("004_") and scalar(
        execute(connection, "SELECT EXISTS (SELECT 1 FROM rag_article_documents)")
    ):
        raise LegacyDocumentError


def migrate(database_url: str, directory: Path | None = None) -> None:
    """Serialize migration runners and commit ledger with schema changes."""
    if directory is None:
        scripts = bundled_migrations()
    else:
        paths = sorted(directory.glob("*.sql"))
        if not paths:
            raise MigrationResourcesError(str(directory))
        scripts = tuple((path.name, path.read_text(encoding="utf-8")) for path in paths)
    engine = create_rag_engine(database_url, pooled=False)
    try:
        with transaction(engine) as connection:
            _apply(connection, scripts)
    finally:
        engine.dispose()


def _apply(connection: Connection, scripts: tuple[tuple[str, str], ...]) -> None:
    _ = execute(connection, f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_KEY})")
    _ = execute(
        connection,
        """CREATE TABLE IF NOT EXISTS rag_schema_migrations (
            filename text PRIMARY KEY, checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT clock_timestamp())""",
    )
    for filename, script in scripts:
        checksum = sha256(script.encode()).hexdigest()
        recorded = scalar(
            execute(
                connection,
                "SELECT checksum FROM rag_schema_migrations WHERE filename = %s",
                (filename,),
            )
        )
        if recorded is not None:
            if recorded != checksum:
                raise MigrationChangedError(filename)
            continue
        _guard_unrecorded(connection, filename)
        _ = execute(connection, script)
        _ = execute(
            connection,
            "INSERT INTO rag_schema_migrations VALUES (%s, %s, DEFAULT)",
            (filename, checksum),
        )
    has_json = scalar(
        execute(
            connection,
            """SELECT EXISTS (SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
            AND table_name = 'rag_article_documents'
            AND column_name = 'document_json')""",
        )
    )
    if has_json and scalar(
        execute(
            connection,
            """SELECT EXISTS (SELECT 1 FROM rag_article_documents
            WHERE document_json IS NULL)""",
        )
    ):
        raise LegacyDocumentError
