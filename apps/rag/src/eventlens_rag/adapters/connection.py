"""Typed PostgreSQL connection boundary."""

from typing import ClassVar

import psycopg
from psycopg.rows import TupleRow

CONNECT_TIMEOUT_SECONDS = 5


class StorageUnavailableError(RuntimeError):
    """RAG database cannot be reached; callers must not treat this as empty data."""

    code: ClassVar[str] = "rag_storage_unavailable"

    def __init__(self) -> None:
        """Hide connection details such as credentials from the message."""
        super().__init__("RAG PostgreSQL storage is unavailable")


def connect(database_url: str) -> psycopg.Connection[TupleRow]:
    """Open one transaction-scoped connection or raise the typed unavailable error."""
    try:
        return psycopg.connect(database_url, connect_timeout=CONNECT_TIMEOUT_SECONDS)
    except psycopg.OperationalError as error:
        raise StorageUnavailableError from error
