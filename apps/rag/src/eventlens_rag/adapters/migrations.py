"""Transactional, checksum-verified migration ledger."""

from hashlib import sha256
from importlib.resources import files
from pathlib import Path

from psycopg import Connection
from psycopg.rows import TupleRow, scalar_row
from pydantic import TypeAdapter

from eventlens_rag.adapters.connection import connect

EXPECTED_MIGRATIONS = (
    "001_enable_vector.sql",
    "002_create_rag_index.sql",
    "003_storage_guards.sql",
    "004_staged_publication.sql",
    "005_current_lexical_document.sql",
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
    directory = files("eventlens_rag").joinpath("migrations")
    if not directory.is_dir() or any(
        not directory.joinpath(name).is_file() for name in EXPECTED_MIGRATIONS
    ):
        raise MigrationResourcesError(str(directory))
    return tuple(
        (name, directory.joinpath(name).read_text(encoding="utf-8"))
        for name in EXPECTED_MIGRATIONS
    )


def _guard_unrecorded(connection: Connection[TupleRow], filename: str) -> None:
    """Fail closed before an unrecorded script meets data it cannot upgrade."""
    with connection.cursor(row_factory=scalar_row) as cursor:
        if filename.startswith("002_"):
            _ = cursor.execute(
                """SELECT table_name::text FROM information_schema.tables
                WHERE table_schema = current_schema()
                AND table_name = ANY(%s) ORDER BY table_name""",
                (list(LEDGER_OWNED_TABLES),),
            )
            conflicts = TypeAdapter(tuple[str, ...]).validate_python(cursor.fetchall())
            if conflicts:
                raise LegacySchemaError(conflicts)
        if filename.startswith("004_"):
            _ = cursor.execute("SELECT EXISTS (SELECT 1 FROM rag_article_documents)")
            if cursor.fetchone():
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
    with connect(database_url) as connection:
        _ = connection.execute("SELECT pg_advisory_xact_lock(734528109)")
        _ = connection.execute(
            """CREATE TABLE IF NOT EXISTS rag_schema_migrations (
                filename text PRIMARY KEY, checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp())"""
        )
        with connection.cursor(row_factory=scalar_row) as cursor:
            for filename, script in scripts:
                checksum = sha256(script.encode()).hexdigest()
                _ = cursor.execute(
                    "SELECT checksum FROM rag_schema_migrations WHERE filename = %s",
                    (filename,),
                )
                recorded = cursor.fetchone()
                if recorded is not None:
                    if recorded != checksum:
                        raise MigrationChangedError(filename)
                    continue
                _guard_unrecorded(connection, filename)
                _ = connection.execute(script.encode("utf-8"))
                _ = connection.execute(
                    "INSERT INTO rag_schema_migrations VALUES (%s, %s, DEFAULT)",
                    (filename, checksum),
                )
            _ = cursor.execute(
                """SELECT EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                AND table_name = 'rag_article_documents'
                AND column_name = 'document_json')"""
            )
            if cursor.fetchone():
                _ = cursor.execute(
                    """SELECT EXISTS (SELECT 1 FROM rag_article_documents
                    WHERE document_json IS NULL)"""
                )
                if cursor.fetchone():
                    raise LegacyDocumentError
