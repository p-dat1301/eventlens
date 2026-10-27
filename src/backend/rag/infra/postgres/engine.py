"""Process-wide SQLAlchemy Engine and short, typed transaction scopes.

psycopg 3 stays the DBAPI driver underneath. The libpq connection string
(URL or ``key=value`` form) is handed to the driver verbatim through the
``do_connect`` hook, so options such as ``search_path`` keep libpq semantics
and the Engine's URL never carries credentials. Each ``transaction()`` is one
connection checkout and one commit-or-rollback; callers never hold it across
model, embedding or LangGraph calls.
"""

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from threading import Lock
from typing import cast

from sqlalchemy import Connection, CursorResult, Engine, create_engine, event, exc
from sqlalchemy.pool import ConnectionPoolEntry, NullPool

from backend.rag.infra.postgres.connection import (
    CONNECT_TIMEOUT_SECONDS,
    StorageOperationError,
    StorageUnavailableError,
)

type Params = Mapping[str, object] | Sequence[object]
type Row = tuple[object, ...]

POOL_SIZE = 5
POOL_OVERFLOW = 5
POOL_RECYCLE_SECONDS = 1800

_engines: dict[str, Engine] = {}
_engines_lock = Lock()


def create_rag_engine(database_url: str, *, pooled: bool = True) -> Engine:
    """Build one Engine for a libpq connection string.

    ``pooled=False`` opens one connection per checkout (migration commands).
    """
    options: dict[str, object] = {}
    if pooled:
        options |= {
            "pool_size": POOL_SIZE,
            "max_overflow": POOL_OVERFLOW,
            "pool_pre_ping": True,
            "pool_recycle": POOL_RECYCLE_SECONDS,
        }
    else:
        options["poolclass"] = NullPool
    engine = create_engine(
        "postgresql+psycopg://",
        # prepare_threshold=None: pooled connections live long, and psycopg's
        # automatic server-side prepare would let PostgreSQL switch hot
        # retrieval statements to generic plans. The pre-SQLAlchemy adapters
        # used one connection per operation and never prepared; keep that.
        connect_args={
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "prepare_threshold": None,
        },
        hide_parameters=True,
        **options,
    )

    def _connect_with_dsn(
        _dialect: object,
        _record: ConnectionPoolEntry,
        cargs: list[object],
        _cparams: dict[str, object],
    ) -> None:
        cargs[:] = [database_url]

    event.listen(engine, "do_connect", _connect_with_dsn)
    return engine


def rag_engine(database_url: str) -> Engine:
    """Return this process's shared pooled Engine for one connection string."""
    with _engines_lock:
        engine = _engines.get(database_url)
        if engine is None:
            engine = create_rag_engine(database_url)
            _engines[database_url] = engine
        return engine


def dispose_engines() -> None:
    """Close every pooled connection (process shutdown and test isolation)."""
    with _engines_lock:
        engines = list(_engines.values())
        _engines.clear()
    for engine in engines:
        engine.dispose()


@contextmanager
def transaction(engine: Engine, *, read_only: bool = False) -> Generator[Connection]:
    """One short transaction: commit on success, roll back on any exception.

    Connection loss (at checkout or mid-statement) becomes
    ``StorageUnavailableError``; any other driver failure becomes
    ``StorageOperationError``. Neither message carries SQL, parameters or the
    connection string; the original driver error stays the ``__cause__``.
    """
    try:
        with engine.begin() as connection:
            if read_only:
                _ = connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            yield connection
    except exc.DBAPIError as error:
        if _is_unavailable(error):
            raise StorageUnavailableError from error.orig
        raise StorageOperationError from error.orig


def _is_unavailable(error: exc.DBAPIError) -> bool:
    """Connection-level failure: lost, refused, or server shutting down.

    Client-side connection errors carry no SQLSTATE; class 08 is connection
    exception and 57P is operator intervention (shutdown, cannot connect now).
    Lock, deadlock and constraint failures stay ``StorageOperationError``.
    """
    if error.connection_invalidated:
        return True
    state = cast("str | None", getattr(error.orig, "sqlstate", None))
    if state is None:
        return isinstance(error, exc.OperationalError)
    return state.startswith(("08", "57P"))


def execute(connection: Connection, sql: str, params: Params = ()) -> CursorResult[Row]:
    """Run reviewed driver-level SQL with bound parameters.

    Used where the exact statement text must be preserved for its query plan
    (FTS, pgvector, ``SKIP LOCKED``, fenced conditional writes). Values are
    always bound by the driver, never interpolated.
    """
    bound: Mapping[str, object] | tuple[object, ...] = (
        params if isinstance(params, Mapping) else tuple(params)
    )
    return cast("CursorResult[Row]", connection.exec_driver_sql(sql, bound or None))


def rows(result: CursorResult[Row]) -> list[Row]:
    """Materialize result rows as plain tuples for pydantic validation."""
    return [tuple(row) for row in result]


def first(result: CursorResult[Row]) -> Row | None:
    """First row as a plain tuple, or ``None``."""
    row = result.fetchone()
    return None if row is None else tuple(row)


def scalar(result: CursorResult[Row]) -> object:
    """First column of the first row, or ``None`` when there is no row."""
    row = first(result)
    return None if row is None else row[0]


def scalars(result: CursorResult[Row]) -> list[object]:
    """First column of every row."""
    return [row[0] for row in result]


def relation_exists(connection: Connection, name: str) -> bool:
    """Whether ``current_schema()`` itself holds a relation of this name.

    Unlike ``to_regclass`` this ignores same-named objects that are only
    visible through later ``search_path`` entries (e.g. ``public``).
    """
    return bool(
        scalar(
            execute(
                connection,
                """SELECT EXISTS (SELECT 1 FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema() AND c.relname = %s)""",
                (name,),
            )
        )
    )
