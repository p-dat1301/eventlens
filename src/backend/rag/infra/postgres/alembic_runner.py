"""Alembic: the single RAG migration authority, plus legacy adoption.

``upgrade_head`` builds empty databases from the baseline and applies later
revisions; it refuses databases created by the historical 001-007 runner
until they are adopted. ``preflight`` inspects such a database read-only;
``adopt_legacy`` re-runs the same checks under the migration advisory lock
and, only when every check passes, stamps the baseline in that transaction.
Stamping writes ``alembic_version`` only; no historical DDL is re-run and the
``rag_schema_migrations`` ledger is left untouched for audit. API and worker
startup never stamp.
"""

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import TypeAdapter
from sqlalchemy import Connection

from backend.rag.infra.postgres.catalog import (
    differences,
    expected_snapshot,
    snapshot,
)
from backend.rag.infra.postgres.engine import (
    create_rag_engine,
    execute,
    relation_exists,
    rows,
    scalar,
    transaction,
)
from backend.rag.infra.postgres.migrations import (
    EXPECTED_MIGRATIONS,
    LEDGER_OWNED_TABLES,
    MIGRATION_LOCK_KEY,
    LegacySchemaError,
)
from backend.rag.infra.postgres.revisions.versions.rag_0001_baseline import (
    LEGACY_CHECKSUMS,
)

SCRIPT_LOCATION = "backend.rag.infra.postgres:revisions"
BASELINE_REVISION = "rag_0001_baseline"

_LEDGER = TypeAdapter(list[tuple[str, str]])
_COUNT = TypeAdapter(int)

# Each query counts rows that would break a runtime invariant after adoption.
DATA_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "revisions without document_json",
        "SELECT count(*) FROM rag_article_documents WHERE document_json IS NULL",
    ),
    (
        "document_json identity differs from its row key",
        """SELECT count(*) FROM rag_article_documents
        WHERE document_json->>'article_id' IS DISTINCT FROM article_id::text
        OR document_json->>'content_hash' IS DISTINCT FROM content_hash""",
    ),
    (
        "tombstoned articles with an active generation",
        """SELECT count(*) FROM rag_article_current
        WHERE tombstoned_at IS NOT NULL AND active_index_generation IS NOT NULL""",
    ),
    (
        "active generation not ACTIVE or not of the current revision",
        """SELECT count(*) FROM rag_article_current cur
        JOIN rag_index_generations g ON g.generation = cur.active_index_generation
        WHERE g.status <> 'ACTIVE' OR g.article_id IS DISTINCT FROM cur.article_id
        OR g.content_hash IS DISTINCT FROM cur.content_hash""",
    ),
    (
        "chunks whose generation belongs to another revision",
        """SELECT count(*) FROM rag_chunks c
        JOIN rag_index_generations g ON g.generation = c.generation
        WHERE g.article_id IS DISTINCT FROM c.article_id
        OR g.content_hash IS DISTINCT FROM c.content_hash""",
    ),
    (
        "publications whose metadata names another revision",
        """SELECT count(*) FROM rag_enrichment_publications
        WHERE metadata->>'article_id' IS DISTINCT FROM article_id::text
        OR metadata->>'content_hash' IS DISTINCT FROM content_hash""",
    ),
)


class LegacyAdoptionRequiredError(RuntimeError):
    """The database was built by the historical runner and is not adopted."""

    def __init__(self) -> None:
        """Point the operator at the explicit, verified adoption command."""
        super().__init__(
            """This RAG database was created by the historical 001-007 runner and
is not managed by Alembic yet. Nothing was changed. Back it up, stop writers,
then run `eventlens-rag preflight` and `eventlens-rag adopt-legacy`."""
        )


class PreflightFailedError(RuntimeError):
    """Adoption refused: the legacy database is not exactly the 007 state."""

    def __init__(self, problems: tuple[str, ...]) -> None:
        """List every failed check; nothing was stamped."""
        self.problems: tuple[str, ...] = problems
        listed = "\n".join(f"- {problem}" for problem in problems)
        super().__init__(
            f"legacy adoption preflight failed; nothing changed:\n{listed}"
        )


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Outcome of the read-only legacy adoption checks."""

    problems: tuple[str, ...]
    row_counts: tuple[tuple[str, int], ...]
    alembic_revision: str | None

    @property
    def ok(self) -> bool:
        """True when every check passed (the database is consistent)."""
        return not self.problems

    @property
    def adoptable(self) -> bool:
        """True when a consistent historical database awaits its stamp."""
        return self.ok and self.alembic_revision is None


def alembic_config(connection: Connection | None = None) -> Config:
    """Programmatic config: packaged revisions, optional shared connection."""
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def head_revision() -> str:
    """Newest packaged revision."""
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        msg = "no packaged Alembic revisions"
        raise RuntimeError(msg)
    return head


def _lock(connection: Connection) -> None:
    _ = execute(connection, f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_KEY})")


def _exists(connection: Connection, relation: str) -> bool:
    return relation_exists(connection, relation)


def current_revision(connection: Connection) -> str | None:
    """Alembic revision recorded in this schema, if any."""
    schema = TypeAdapter(str).validate_python(
        scalar(execute(connection, "SELECT current_schema()"))
    )
    return MigrationContext.configure(
        connection, opts={"version_table_schema": schema}
    ).get_current_revision()


def upgrade_head(database_url: str) -> str:
    """Apply every packaged revision; fail closed on unadopted legacy schemas."""
    engine = create_rag_engine(database_url, pooled=False)
    try:
        with transaction(engine) as connection:
            _lock(connection)
            if not _exists(connection, "alembic_version"):
                if _exists(connection, "rag_schema_migrations"):
                    raise LegacyAdoptionRequiredError
                conflicts = tuple(
                    table for table in LEDGER_OWNED_TABLES if _exists(connection, table)
                )
                if conflicts:
                    raise LegacySchemaError(conflicts)
            command.upgrade(alembic_config(connection), "head")
            revision = current_revision(connection)
    finally:
        engine.dispose()
    return revision or ""


def _ledger_problems(connection: Connection) -> list[str]:
    problems: list[str] = []
    expected = dict(LEGACY_CHECKSUMS)
    if tuple(expected) != EXPECTED_MIGRATIONS:
        problems.append("baseline checksum list does not name 001-007 in order")
    directory = files("backend.rag.infra.postgres").joinpath("migrations")
    for name, checksum in LEGACY_CHECKSUMS:
        resource = directory.joinpath(name)
        if not resource.is_file():
            problems.append(f"bundled SQL missing: {name}")
        elif sha256(resource.read_bytes()).hexdigest() != checksum:
            problems.append(f"bundled SQL bytes changed: {name}")
    if not _exists(connection, "rag_schema_migrations"):
        problems.append("rag_schema_migrations ledger missing")
        return problems
    recorded = dict(
        _LEDGER.validate_python(
            rows(
                execute(
                    connection,
                    "SELECT filename, checksum FROM rag_schema_migrations",
                )
            )
        )
    )
    problems.extend(
        f"ledger missing {name}" for name in expected if name not in recorded
    )
    problems.extend(
        f"ledger has unexpected {name}" for name in recorded if name not in expected
    )
    problems.extend(
        f"ledger checksum mismatch: {name}"
        for name, checksum in recorded.items()
        if name in expected and checksum != expected[name]
    )
    return problems


def _data_problems(connection: Connection) -> list[str]:
    problems: list[str] = []
    for label, query in DATA_CHECKS:
        count = _COUNT.validate_python(scalar(execute(connection, query)))
        if count:
            problems.append(f"data: {count} {label}")
    return problems


def _row_counts(connection: Connection) -> tuple[tuple[str, int], ...]:
    tables = sorted(
        table
        for table in (
            *LEDGER_OWNED_TABLES,
            "rag_enrichment_versions",
            "rag_enrichment_publications",
            "rag_source_references",
            "rag_schema_migrations",
        )
        if _exists(connection, table)
    )
    return tuple(
        (
            table,
            _COUNT.validate_python(
                scalar(execute(connection, f'SELECT count(*) FROM "{table}"'))  # noqa: S608
            ),
        )
        for table in tables
    )


def _inspect(connection: Connection) -> PreflightReport:
    """Run every check; being Alembic-managed is state, not a problem.

    The packaged catalog describes the baseline, so it is compared only while
    the database is unmanaged or exactly at the baseline. Ledger and data
    invariants hold at every revision. An unknown recorded revision fails.
    """
    revision = (
        current_revision(connection) if _exists(connection, "alembic_version") else None
    )
    problems = _ledger_problems(connection)
    if revision is not None and revision not in _known_revisions():
        problems.append(f"unknown alembic revision {revision}")
    if not problems and revision in {None, BASELINE_REVISION}:
        problems.extend(differences(snapshot(connection), expected_snapshot()))
    if not problems:
        problems.extend(_data_problems(connection))
    return PreflightReport(
        problems=tuple(problems),
        row_counts=_row_counts(connection),
        alembic_revision=revision,
    )


def _known_revisions() -> set[str]:
    script = ScriptDirectory.from_config(alembic_config())
    return {item.revision for item in script.walk_revisions()}


def preflight(database_url: str) -> PreflightReport:
    """Read-only adoption checks: ledger, raw-byte checksums, catalog, data."""
    engine = create_rag_engine(database_url, pooled=False)
    try:
        with transaction(engine, read_only=True) as connection:
            return _inspect(connection)
    finally:
        engine.dispose()


def adopt_legacy(database_url: str) -> PreflightReport:
    """Verify under the migration lock, then stamp the baseline atomically.

    Idempotent: re-running on an adopted database re-checks it and returns
    the same consistent report without stamping again. Any failed check,
    adopted or not, raises ``PreflightFailedError`` and changes nothing.
    """
    engine = create_rag_engine(database_url, pooled=False)
    try:
        with transaction(engine) as connection:
            _lock(connection)
            report = _inspect(connection)
            if report.problems:
                raise PreflightFailedError(report.problems)
            if report.alembic_revision is not None:
                return report
            command.stamp(alembic_config(connection), BASELINE_REVISION)
            return PreflightReport(
                problems=(),
                row_counts=report.row_counts,
                alembic_revision=current_revision(connection),
            )
    finally:
        engine.dispose()
