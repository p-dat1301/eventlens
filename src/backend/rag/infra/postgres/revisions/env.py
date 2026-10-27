"""Alembic environment for the RAG schema.

The connection string comes from ``config.attributes["database_url"]`` (the
``eventlens-rag`` CLI), ``-x database_url=...`` or
``EVENTLENS_RAG_DATABASE_URL``. A caller may instead pass an open connection
in ``config.attributes["connection"]`` (adoption runs preflight and stamp in
one transaction). Every run holds the advisory lock the historical runner
uses, so the two can never interleave on one database. Online mode only.
"""

import os

from alembic import context
from sqlalchemy import Connection

from backend.rag.infra.postgres.engine import create_rag_engine, execute, scalar
from backend.rag.infra.postgres.migrations import MIGRATION_LOCK_KEY
from backend.rag.infra.postgres.models import Base

DATABASE_URL_ENV = "EVENTLENS_RAG_DATABASE_URL"


def _database_url() -> str:
    config = context.config
    value = (
        config.attributes.get("database_url")
        or context.get_x_argument(as_dictionary=True).get("database_url")
        or os.environ.get(DATABASE_URL_ENV, "")
    )
    if not isinstance(value, str) or not value.strip():
        msg = f"RAG database URL missing: set {DATABASE_URL_ENV}"
        raise RuntimeError(msg)
    return value


def _run(connection: Connection) -> None:
    # Pin alembic_version to current_schema(): a same-named table that is only
    # visible through a later search_path entry must never be mistaken for it.
    schema = scalar(execute(connection, "SELECT current_schema()"))
    context.configure(
        connection=connection,
        version_table_schema=str(schema),
        target_metadata=Base.metadata,
        transactional_ddl=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        _ = execute(connection, f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_KEY})")
        context.run_migrations()


def run_online() -> None:
    """Run against a provided connection or a fresh non-pooled Engine."""
    provided = context.config.attributes.get("connection")
    if isinstance(provided, Connection):
        _run(provided)
        return
    engine = create_rag_engine(_database_url(), pooled=False)
    try:
        with engine.connect() as connection:
            _run(connection)
            connection.commit()
    finally:
        engine.dispose()


if context.is_offline_mode():
    msg = "offline SQL generation is not supported for the RAG schema"
    raise RuntimeError(msg)
run_online()
