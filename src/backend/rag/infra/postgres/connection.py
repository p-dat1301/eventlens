"""Typed PostgreSQL storage errors shared by every RAG adapter."""

from typing import ClassVar

CONNECT_TIMEOUT_SECONDS = 5


class StorageUnavailableError(RuntimeError):
    """RAG database cannot be reached; callers must not treat this as empty data."""

    code: ClassVar[str] = "rag_storage_unavailable"

    def __init__(self) -> None:
        """Hide connection details such as credentials from the message."""
        super().__init__("RAG PostgreSQL storage is unavailable")


class StorageOperationError(RuntimeError):
    """A statement failed and its transaction was rolled back.

    The message never carries SQL, bound values or the connection string; the
    driver error is kept as ``__cause__`` for operators.
    """

    code: ClassVar[str] = "rag_storage_failed"

    def __init__(self) -> None:
        """Generic message; nothing from the failed statement is included."""
        super().__init__("RAG PostgreSQL statement failed; transaction rolled back")
