"""CLI boundary maps a failed, rolled-back statement to a stable typed exit.

A trigger raises inside each command's write transaction. Every command must
exit 4 with ``rag_storage_failed``, print no traceback, SQL or credentials,
and leave nothing half-written.
"""

from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import TypeAdapter
from typer.testing import CliRunner, Result

from backend.rag.cli_main import STORAGE_FAILED_EXIT
from backend.rag.cli_main import app as rag_app
from backend.rag.worker_main import app as worker_app
from schema_support import prepare_schema

BUNDLE = Path(__file__).parents[4] / "fixtures/article_bundle.v1.json"


@pytest.fixture
def url(database_url: str) -> Generator[str]:
    schema = "cli_" + uuid4().hex
    with psycopg.connect(database_url, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    scoped = make_conninfo(database_url, options=f"-c search_path={schema},public")
    prepare_schema(scoped)
    yield scoped
    with psycopg.connect(database_url, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )


def _fail_inserts(url: str, table: str) -> None:
    with psycopg.connect(url) as connection:
        _ = connection.execute(
            """CREATE FUNCTION cli_fault() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected cli fault'; END; $$"""
        )
        _ = connection.execute(
            sql.SQL(
                """CREATE TRIGGER cli_fault BEFORE INSERT ON {}
                FOR EACH ROW EXECUTE FUNCTION cli_fault()"""
            ).format(sql.Identifier(table))
        )


def _count(url: str, table: str) -> int:
    with psycopg.connect(url) as connection:
        row = connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone()
    assert row is not None
    return TypeAdapter(int).validate_python(row[0])


def _assert_typed_failure(result: Result, url: str) -> None:
    password = str(conninfo_to_dict(url).get("password", ""))
    assert result.exit_code == STORAGE_FAILED_EXIT, result.output
    assert isinstance(result.exception, SystemExit)
    assert result.output.startswith("rag_storage_failed: "), result.output
    assert "Traceback" not in result.output
    assert "INSERT" not in result.output
    assert "injected cli fault" not in result.output
    assert not password or password not in result.output


def test_worker_ingest_failure_is_typed_and_rolled_back(url: str) -> None:
    _fail_inserts(url, "rag_enrichment_jobs")
    result = CliRunner().invoke(
        worker_app, ["ingest", str(BUNDLE), "--database-url", url]
    )
    _assert_typed_failure(result, url)
    assert _count(url, "rag_article_documents") == 0


def test_worker_enrich_failure_is_typed_and_publishes_nothing(url: str) -> None:
    runner = CliRunner()
    ingested = runner.invoke(worker_app, ["ingest", str(BUNDLE), "--database-url", url])
    assert ingested.exit_code == 0, ingested.output
    _fail_inserts(url, "rag_chunks")
    result = runner.invoke(
        worker_app, ["enrich", "--database-url", url, "--max-jobs", "1"]
    )
    _assert_typed_failure(result, url)
    assert _count(url, "rag_index_generations") == 0
    assert _count(url, "rag_enrichment_publications") == 0


def test_load_fixtures_failure_is_typed(url: str) -> None:
    _fail_inserts(url, "rag_article_documents")
    result = CliRunner().invoke(rag_app, ["load-fixtures", "--database-url", url])
    _assert_typed_failure(result, url)
    assert _count(url, "rag_article_documents") == 0


def test_rebuild_failure_is_typed_and_activates_nothing(url: str) -> None:
    runner = CliRunner()
    loaded = runner.invoke(rag_app, ["load-fixtures", "--database-url", url])
    assert loaded.exit_code == 0, loaded.output
    _fail_inserts(url, "rag_chunks")
    result = runner.invoke(rag_app, ["rebuild", "--database-url", url])
    _assert_typed_failure(result, url)
    assert _count(url, "rag_chunks") == 0
    assert _count(url, "rag_current_vectors") == 0
