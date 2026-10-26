"""Alembic authority: fresh build, verified adoption of 001-007 DBs, fail-closed.

Every case runs on real PostgreSQL + pgvector in its own schema. The
historical runner (``migrations.migrate``) builds the legacy databases; it is
never run on a database Alembic manages.
"""

import os
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from typing import cast
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from psycopg import sql
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

from backend.rag.cli_main import app
from backend.rag.infra.postgres.alembic_runner import (
    BASELINE_REVISION,
    LegacyAdoptionRequiredError,
    PreflightFailedError,
    adopt_legacy,
    alembic_config,
    preflight,
    upgrade_head,
)
from backend.rag.infra.postgres.catalog import (
    differences,
    expected_snapshot,
    snapshot,
)
from backend.rag.infra.postgres.engine import rag_engine, transaction
from backend.rag.infra.postgres.migrations import (
    EXPECTED_MIGRATIONS,
    AlembicManagedError,
    migrate,
)
from backend.rag.infra.postgres.models import Base
from backend.rag.infra.postgres.revisions.versions.rag_0001_baseline import (
    LEGACY_CHECKSUMS,
)
from legacy_seed import fingerprint, seed

SQL_DIR = Path(__file__).parents[4] / "src/backend/rag/infra/postgres/migrations"
type NewSchema = Callable[[], str]


@pytest.fixture
def new_schema() -> Generator[NewSchema]:
    base = os.environ.get("EVENTLENS_RAG_DATABASE_URL")
    if base is None:
        pytest.skip("EVENTLENS_RAG_DATABASE_URL required for real PostgreSQL test")
    created: list[str] = []

    def make() -> str:
        schema = "alembic_" + uuid4().hex
        with psycopg.connect(base, autocommit=True) as connection:
            _ = connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )
            _ = connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        created.append(schema)
        return make_conninfo(base, options=f"-c search_path={schema},public")

    yield make
    with psycopg.connect(base, autocommit=True) as connection:
        for schema in created:
            _ = connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def _catalog(url: str) -> dict[str, list[list[str]]]:
    with transaction(rag_engine(url), read_only=True) as connection:
        return snapshot(connection)


def _has_alembic(url: str) -> bool:
    with psycopg.connect(url) as connection:
        row = connection.execute(
            """SELECT EXISTS (SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relname = 'alembic_version')"""
        ).fetchone()
    return row == (True,)


def _run(url: str, statement: str) -> None:
    with psycopg.connect(url) as connection:
        _ = connection.execute(statement.encode())


def _ledger(url: str) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as connection:
        return list(
            connection.execute(
                """SELECT filename, checksum, applied_at FROM rag_schema_migrations
                ORDER BY filename"""
            ).fetchall()
        )


def _legacy(new_schema: NewSchema, *, seeded: bool = True) -> str:
    url = new_schema()
    migrate(url, SQL_DIR)
    if seeded:
        _ = seed(url)
    return url


# --- fresh databases -------------------------------------------------------


def test_fresh_upgrade_equals_historical_runner_catalog(new_schema: NewSchema) -> None:
    # Given: one schema per tool, both from empty.
    fresh, legacy = new_schema(), new_schema()

    # When
    assert upgrade_head(fresh) == BASELINE_REVISION
    migrate(legacy, SQL_DIR)

    # Then: identical deparsed catalog, equal to the packaged expectation.
    assert differences(_catalog(fresh), _catalog(legacy)) == []
    assert differences(_catalog(fresh), expected_snapshot()) == []


def test_fresh_ledger_holds_the_seven_raw_byte_checksums(new_schema: NewSchema) -> None:
    url = new_schema()
    _ = upgrade_head(url)
    recorded = [(name, digest) for name, digest, _ in _ledger(url)]
    on_disk = [
        (name, sha256((SQL_DIR / name).read_bytes()).hexdigest())
        for name in EXPECTED_MIGRATIONS
    ]
    assert recorded == on_disk == list(LEGACY_CHECKSUMS)


def test_historical_runner_only_verifies_an_alembic_database(
    new_schema: NewSchema, tmp_path: Path
) -> None:
    # Given: Alembic-built schema.
    url = new_schema()
    _ = upgrade_head(url)
    before = _ledger(url)

    # When/Then: the bundled 001-007 are recorded, so the runner is a no-op;
    # a new script is refused instead of applied.
    migrate(url)
    assert _ledger(url) == before
    for path in SQL_DIR.glob("*.sql"):
        _ = (tmp_path / path.name).write_bytes(path.read_bytes())
    _ = (tmp_path / "008_extra.sql").write_text("CREATE TABLE rag_x (id int);")
    with pytest.raises(AlembicManagedError):
        migrate(url, tmp_path)
    assert _ledger(url) == before


def test_migrate_repeats_without_changes(new_schema: NewSchema) -> None:
    url = new_schema()
    runner = CliRunner()
    first = runner.invoke(app, ["migrate", "--database-url", url])
    catalog = _catalog(url)
    ledger = _ledger(url)
    second = runner.invoke(app, ["migrate", "--database-url", url])
    assert first.exit_code == second.exit_code == 0, first.output + second.output
    assert f"alembic {BASELINE_REVISION}" in second.output
    assert _catalog(url) == catalog
    assert _ledger(url) == ledger


def test_orm_metadata_has_no_drift_from_fresh_database(new_schema: NewSchema) -> None:
    url = new_schema()
    _ = upgrade_head(url)
    with rag_engine(url).connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "include_name": _mapped_only}
        )
        diff = cast("list[object]", compare_metadata(context, Base.metadata))
    assert diff == []


def _mapped_only(name: str | None, kind: str, _parent: object) -> bool:
    """Compare the eight mapped tables; ledger and alembic_version are not ORM."""
    return kind != "table" or name in Base.metadata.tables


def test_concurrent_fresh_migrations_serialize(new_schema: NewSchema) -> None:
    url = new_schema()
    with ThreadPoolExecutor(3) as pool:
        revisions = list(pool.map(upgrade_head, [url] * 3))
    assert revisions == [BASELINE_REVISION] * 3
    assert len(_ledger(url)) == 7
    assert differences(_catalog(url), expected_snapshot()) == []


def test_baseline_downgrade_is_refused(new_schema: NewSchema) -> None:
    url = new_schema()
    _ = upgrade_head(url)
    with (
        pytest.raises(RuntimeError, match="cannot be downgraded"),
        transaction(rag_engine(url)) as connection,
    ):
        command.downgrade(alembic_config(connection), "base")
    assert _has_alembic(url)
    assert len(_ledger(url)) == 7


# --- legacy databases ------------------------------------------------------


def test_unadopted_legacy_database_is_refused_unchanged(new_schema: NewSchema) -> None:
    url = _legacy(new_schema)
    before = fingerprint(url)
    with pytest.raises(LegacyAdoptionRequiredError):
        _ = upgrade_head(url)
    result = CliRunner().invoke(app, ["migrate", "--database-url", url])
    assert result.exit_code == 1
    assert "rag_migration_failed" in result.output
    assert "adopt-legacy" in result.output
    assert not _has_alembic(url)
    assert fingerprint(url) == before


def test_adopt_seeded_legacy_keeps_rows_schema_and_ledger(
    new_schema: NewSchema,
) -> None:
    # Given: a populated database built by the historical runner.
    url = _legacy(new_schema)
    rows_before = fingerprint(url)
    ledger_before = _ledger(url)
    assert rows_before["rag_enrichment_publications"][0] > 0
    assert rows_before["rag_source_references"][0] == 2

    # When: read-only preflight, explicit adoption, then the normal migrate.
    report = preflight(url)
    runner = CliRunner()
    checked = runner.invoke(app, ["preflight", "--database-url", url])
    adopted = runner.invoke(app, ["adopt-legacy", "--database-url", url])
    migrated = runner.invoke(app, ["migrate", "--database-url", url])
    again = adopt_legacy(url)

    # Then: nothing but alembic_version changed.
    assert report.adoptable, report.problems
    assert checked.exit_code == adopted.exit_code == migrated.exit_code == 0
    assert again.alembic_revision == BASELINE_REVISION
    assert fingerprint(url) == rows_before
    assert _ledger(url) == ledger_before
    assert differences(_catalog(url), expected_snapshot()) == []
    assert dict(report.row_counts)["rag_article_documents"] == 32


def test_adoption_rolls_back_when_stamping_fails(new_schema: NewSchema) -> None:
    url = _legacy(new_schema, seeded=False)
    real_stamp = command.stamp

    def stamp_then_fail(config: object, revision: str) -> None:
        real_stamp(config, revision)  # pyright: ignore[reportArgumentType]
        msg = "simulated failure after stamp"
        raise RuntimeError(msg)

    with (
        patch.object(command, "stamp", stamp_then_fail),
        pytest.raises(RuntimeError, match="simulated"),
    ):
        _ = adopt_legacy(url)
    assert not _has_alembic(url)
    assert preflight(url).adoptable


type Breakage = tuple[str, str]

BREAKAGES: dict[str, Breakage] = {
    "checksum": (
        """UPDATE rag_schema_migrations SET checksum = repeat('0', 64)
        WHERE filename = '003_storage_guards.sql'""",
        "ledger checksum mismatch: 003_storage_guards.sql",
    ),
    "missing ledger row": (
        """DELETE FROM rag_schema_migrations
        WHERE filename = '007_source_references.sql'""",
        "ledger missing 007_source_references.sql",
    ),
    "extra ledger row": (
        "INSERT INTO rag_schema_migrations VALUES ('008_local.sql', 'x', DEFAULT)",
        "ledger has unexpected 008_local.sql",
    ),
    "missing hnsw index": (
        "DROP INDEX rag_chunks_embedding_hnsw_idx",
        "missing ['rag_chunks', 'rag_chunks_embedding_hnsw_idx'",
    ),
    "missing gin index": (
        "DROP INDEX rag_article_documents_fts_idx",
        "rag_article_documents_fts_idx",
    ),
    "missing trigger": (
        """DROP TRIGGER rag_enrichment_publication_immutable
        ON rag_enrichment_publications""",
        "triggers: missing",
    ),
    "missing view": ("DROP VIEW rag_current_vectors", "views: missing"),
    "changed check": (
        "ALTER TABLE rag_enrichment_jobs DROP CONSTRAINT rag_job_status_check",
        "rag_job_status_check",
    ),
    "extra column": (
        "ALTER TABLE rag_chunks ADD COLUMN note text",
        "unexpected ['rag_chunks'",
    ),
    "tombstone keeps vectors": (
        """UPDATE rag_article_current SET tombstoned_at = now()
        WHERE active_index_generation IS NOT NULL
        AND article_id = (SELECT min(article_id::text)::uuid FROM rag_article_current
            WHERE active_index_generation IS NOT NULL)""",
        "tombstoned articles with an active generation",
    ),
    "null document_json": (
        """ALTER TABLE rag_article_documents DISABLE TRIGGER rag_article_immutable;
        UPDATE rag_article_documents SET document_json = NULL
        WHERE article_id = (SELECT min(article_id::text)::uuid
            FROM rag_article_documents);
        ALTER TABLE rag_article_documents ENABLE TRIGGER rag_article_immutable""",
        "revisions without document_json",
    ),
}


@pytest.mark.parametrize("case", sorted(BREAKAGES))
def test_preflight_fails_closed(new_schema: NewSchema, case: str) -> None:
    # Given: a seeded legacy database with one defect.
    url = _legacy(new_schema)
    statement, expected = BREAKAGES[case]
    _run(url, statement)
    before = fingerprint(url)

    # When
    report = preflight(url)
    with pytest.raises(PreflightFailedError) as refused:
        _ = adopt_legacy(url)
    result = CliRunner().invoke(app, ["adopt-legacy", "--database-url", url])

    # Then: named problem, nothing stamped, nothing changed.
    assert not report.ok
    assert any(expected in problem for problem in report.problems), report.problems
    assert refused.value.problems == report.problems
    assert result.exit_code == 1
    assert "rag_adoption_refused" in result.output
    assert not _has_alembic(url)
    assert fingerprint(url) == before


def test_untracked_tables_without_ledger_are_refused(new_schema: NewSchema) -> None:
    url = new_schema()
    _run(url, "CREATE TABLE rag_chunks (chunk_id uuid PRIMARY KEY)")
    result = CliRunner().invoke(app, ["migrate", "--database-url", url])
    assert result.exit_code == 1
    assert "Untracked legacy RAG tables" in result.output
    assert not _has_alembic(url)


def _revision(url: str) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as connection:
        return list(connection.execute("SELECT version_num FROM alembic_version"))


def _adopted(new_schema: NewSchema) -> str:
    url = _legacy(new_schema)
    _ = adopt_legacy(url)
    return url


def test_rerun_on_consistent_adopted_database_repeats_the_same_report(
    new_schema: NewSchema,
) -> None:
    # Given: a seeded legacy database adopted once through the CLI.
    url = _legacy(new_schema)
    runner = CliRunner()
    first = runner.invoke(app, ["adopt-legacy", "--database-url", url])
    rows = fingerprint(url)

    # When: both commands run again on the adopted database.
    second = runner.invoke(app, ["adopt-legacy", "--database-url", url])
    checked = runner.invoke(app, ["preflight", "--database-url", url])

    # Then: same consistent report, exit 0, nothing stamped twice or changed.
    assert first.exit_code == second.exit_code == checked.exit_code == 0
    assert first.output == second.output
    assert '"ok": true' in checked.output
    assert '"adoptable": false' in checked.output
    assert f'"alembic_revision": "{BASELINE_REVISION}"' in checked.output
    assert _revision(url) == [(BASELINE_REVISION,)]
    assert fingerprint(url) == rows


DRIFT_AFTER_ADOPTION: dict[str, Breakage] = {
    "checksum": (BREAKAGES["checksum"][0], BREAKAGES["checksum"][1]),
    "missing hnsw index": BREAKAGES["missing hnsw index"],
    "tombstone keeps vectors": BREAKAGES["tombstone keeps vectors"],
    "unknown revision": (
        "UPDATE alembic_version SET version_num = 'rag_9999_unknown'",
        "unknown alembic revision rag_9999_unknown",
    ),
}


@pytest.mark.parametrize("case", sorted(DRIFT_AFTER_ADOPTION))
def test_rerun_on_drifted_adopted_database_fails_closed(
    new_schema: NewSchema, case: str
) -> None:
    # Given: an adopted database that drifted afterwards.
    url = _adopted(new_schema)
    statement, expected = DRIFT_AFTER_ADOPTION[case]
    _run(url, statement)
    revision = _revision(url)
    before = fingerprint(url)

    # When
    runner = CliRunner()
    checked = runner.invoke(app, ["preflight", "--database-url", url])
    adopted = runner.invoke(app, ["adopt-legacy", "--database-url", url])

    # Then: non-zero, the problem is named, nothing changes.
    assert checked.exit_code == 1
    assert '"ok": false' in checked.output
    assert expected in checked.output
    assert adopted.exit_code == 1
    assert "rag_adoption_refused" in adopted.output
    assert expected in adopted.output
    with pytest.raises(PreflightFailedError):
        _ = adopt_legacy(url)
    assert _revision(url) == revision
    assert fingerprint(url) == before
