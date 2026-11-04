"""Guard and row counts for offline evaluation databases (never dev/prod).

Benchmarks and handoff imports only write databases whose name ends in
``_eval`` (the owned ``rag_eval`` container of ``make search-eval``); any other
database is refused before a single statement writes.
"""

from typing import ClassVar, Final

from sqlalchemy import func, select

from backend.rag.infra.postgres.engine import rag_engine, transaction
from backend.rag.infra.postgres.models import (
    ArticleCurrentRow,
    ArticleDocumentRow,
    ChunkRow,
    EnrichmentJobRow,
    SourceVersionRow,
)

EVAL_DB_SUFFIX: Final = "_eval"
_OUTBOX: Final = "rag_search_outbox"


class EvalDatabaseRefusedError(RuntimeError):
    """The target database is not an evaluation database."""

    code: ClassVar[str] = "eval_database_refused"

    def __init__(self, name: str) -> None:
        """Name the refused database (no credentials)."""
        super().__init__(
            f"refusing database {name!r}; only *{EVAL_DB_SUFFIX} databases are written"
        )


def require_eval_database(database_url: str) -> None:
    """Raise unless ``current_database()`` ends in ``_eval``."""
    with transaction(rag_engine(database_url), read_only=True) as connection:
        name = str(connection.scalar(select(func.current_database())))
    if not name.endswith(EVAL_DB_SUFFIX):
        raise EvalDatabaseRefusedError(name)


def table_counts(database_url: str) -> dict[str, int]:
    """Row counts of the import-relevant tables (no content)."""
    tables = (
        ArticleDocumentRow,
        ArticleCurrentRow,
        SourceVersionRow,
        EnrichmentJobRow,
        ChunkRow,
    )
    with transaction(rag_engine(database_url), read_only=True) as connection:
        counts = {
            str(table.__tablename__): int(
                connection.scalar(select(func.count()).select_from(table)) or 0
            )
            for table in tables
        }
        outbox = connection.exec_driver_sql(f"SELECT count(*) FROM {_OUTBOX}")  # noqa: S608 - constant
        counts[_OUTBOX] = int(outbox.scalar() or 0)
    return counts
