import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

from eventlens_rag.adapters.migrations import (
    EXPECTED_MIGRATIONS,
    LegacyDocumentError,
    MigrationResourcesError,
    migrate,
)
from eventlens_rag.cli import app
from eventlens_rag.domain.article_document import ArticleDocument


def test_migrate_rejects_missing_sql_directory(
    database_url: str, tmp_path: Path
) -> None:
    # Given: an absent migration directory.
    missing = tmp_path / "absent"
    # When/Then: migration fails rather than reporting success.
    with pytest.raises(MigrationResourcesError):
        migrate(database_url, missing)


def test_cli_load_fixtures_rejects_nonexistent_directory(tmp_path: Path) -> None:
    # Given: no fixture directory.
    missing = tmp_path / "absent"
    # When: CLI attempts loading.
    result = CliRunner().invoke(
        app, ["load-fixtures", "--database-url", "unused", "--directory", str(missing)]
    )
    # Then: nonzero exit and no false success message.
    assert result.exit_code != 0
    assert "rag persisted" not in result.output


def test_wheel_migrates_outside_checkout(database_url: str, tmp_path: Path) -> None:
    # Given: a built wheel, outside checkout.
    project = Path(__file__).parents[1]
    uv = shutil.which("uv")
    assert uv is not None
    _ = subprocess.run(  # noqa: S603
        [uv, "build", "--wheel", "--out-dir", str(tmp_path), str(project)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("eventlens_rag-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        scripts = sorted(
            name
            for name in archive.namelist()
            if name.startswith("eventlens_rag/migrations/") and name.endswith(".sql")
        )
    assert len(scripts) == 5
    # When: run packaged migration against fresh isolated schema outside checkout.
    schema = "wheel_" + uuid4().hex
    with psycopg.connect(database_url) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    scoped_url = make_conninfo(database_url, options=f"-c search_path={schema},public")
    probe = "from eventlens_rag.adapters.migrations import migrate; "
    probe += "import os; migrate(os.environ['EVENTLENS_RAG_DATABASE_URL'])"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            probe,
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(wheel),
            "EVENTLENS_RAG_DATABASE_URL": scoped_url,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    # Then: packaged SQL applied and recorded, independently from checkout.
    assert result.returncode == 0, result.stderr
    with psycopg.connect(scoped_url) as connection:
        count = connection.execute(
            "SELECT count(*) FROM rag_schema_migrations"
        ).fetchone()
    assert count == (5,)


def test_bundled_sql_preserves_original_migration_bytes() -> None:
    # Given: original checked-in scripts and wheel-owned copies.
    project = Path(__file__).parents[1]
    original = project / "migrations"
    bundled = project / "src/eventlens_rag/migrations"
    # When/Then: same file set and byte-for-byte identity keep checksums stable.
    names = sorted(path.name for path in original.glob("*.sql"))
    assert names == sorted(path.name for path in bundled.glob("*.sql"))
    assert tuple(names) == EXPECTED_MIGRATIONS
    for path in sorted(original.glob("*.sql")):
        assert (bundled / path.name).read_bytes() == path.read_bytes()


def test_populated_pre004_upgrade_fails_before_ledger_success(
    database_url: str, tmp_path: Path, make_article: Callable[[str], ArticleDocument]
) -> None:
    # Given: isolated schema at 003 containing an article without document_json.
    schema = "legacy_" + uuid4().hex
    with psycopg.connect(database_url) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    scoped_url = make_conninfo(database_url, options=f"-c search_path={schema},public")
    source = Path(__file__).parents[1] / "src/eventlens_rag/migrations"
    for path in sorted(source.glob("00[1-3]_*.sql")):
        _ = (tmp_path / path.name).write_bytes(path.read_bytes())
    migrate(scoped_url, tmp_path)
    article = make_article("Legacy title and body cannot reconstruct source URL")
    with psycopg.connect(scoped_url) as connection:
        _ = connection.execute(
            """INSERT INTO rag_article_documents
            (article_id, content_hash, source_id, title, normalized_content)
            VALUES (%s, %s, %s, %s, %s)""",
            (
                article.article_id,
                article.content_hash,
                article.source_id,
                article.title,
                article.normalized_content,
            ),
        )
        _ = connection.execute(
            """INSERT INTO rag_article_current (article_id, content_hash)
            VALUES (%s, %s)""",
            (article.article_id, article.content_hash),
        )

    # When: full upgrade reaches incompatible legacy data.
    with pytest.raises(LegacyDocumentError, match="ArticleDocument"):
        migrate(scoped_url, source)
    # Then: 004/005 not recorded; transaction rolled back.
    with psycopg.connect(scoped_url) as connection:
        rows = connection.execute(
            "SELECT filename FROM rag_schema_migrations ORDER BY filename"
        ).fetchall()
    assert len(rows) == 3


def test_migrate_rejects_existing_null_document_json(database_url: str) -> None:
    # Given: already migrated schema containing incomplete document revision.
    schema = "null_" + uuid4().hex
    with psycopg.connect(database_url) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    scoped_url = make_conninfo(database_url, options=f"-c search_path={schema},public")
    migrate(scoped_url)
    with psycopg.connect(scoped_url) as connection:
        _ = connection.execute(
            """INSERT INTO rag_article_documents
            (article_id, content_hash, source_id, title, normalized_content)
            VALUES (%s, %s, %s, %s, %s)""",
            (uuid4(), "legacy-null", uuid4(), "Legacy", "Lost source data"),
        )
    # When/Then: rerunning migration rejects NULL instead of claiming success.
    with pytest.raises(LegacyDocumentError, match="approved source"):
        migrate(scoped_url)
