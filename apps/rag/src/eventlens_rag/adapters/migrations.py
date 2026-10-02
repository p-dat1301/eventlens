"""Transactional, checksum-verified migration ledger."""

from hashlib import sha256
from pathlib import Path

import psycopg
from psycopg.rows import scalar_row


class MigrationChangedError(RuntimeError):
    """Previously applied migration differs from its recorded checksum."""

    def __init__(self, filename: str) -> None:
        """Identify the historical migration requiring investigation."""
        super().__init__(f"migration checksum mismatch: {filename}")


def migrate(database_url: str, directory: Path) -> None:
    """Serialize migration runners and commit ledger with schema changes."""
    with psycopg.connect(database_url) as connection:
        _ = connection.execute("SELECT pg_advisory_xact_lock(734528109)")
        _ = connection.execute(
            """CREATE TABLE IF NOT EXISTS rag_schema_migrations (
                filename text PRIMARY KEY, checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp())"""
        )
        with connection.cursor(row_factory=scalar_row) as cursor:
            for path in sorted(directory.glob("*.sql")):
                script = path.read_text(encoding="utf-8")
                checksum = sha256(script.encode()).hexdigest()
                _ = cursor.execute(
                    "SELECT checksum FROM rag_schema_migrations WHERE filename = %s",
                    (path.name,),
                )
                recorded = cursor.fetchone()
                if recorded is not None:
                    if recorded != checksum:
                        raise MigrationChangedError(path.name)
                    continue
                _ = connection.execute(script.encode("utf-8"))
                _ = connection.execute(
                    "INSERT INTO rag_schema_migrations VALUES (%s, %s, DEFAULT)",
                    (path.name, checksum),
                )
