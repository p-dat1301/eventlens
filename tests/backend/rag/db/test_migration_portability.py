import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

from backend.rag.cli_main import app
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.infra.postgres.migrations import (
    EXPECTED_MIGRATIONS,
    LegacyDocumentError,
    MigrationResourcesError,
    migrate,
)
from backend.rag.infra.postgres.revisions.versions.rag_0001_baseline import (
    LEGACY_CHECKSUMS,
)

MIGRATION_SOURCE = "src/backend/rag/infra/postgres/migrations"
PACKAGED_PREFIX = "backend/rag/infra/postgres/migrations/"
# Recorded by pre-refactor ledgers; any byte change is a checksum mismatch.
RECORDED_CHECKSUMS = {
    "001_enable_vector.sql": (
        "9e9b2cfec47519f49ee73cb533c459e22f8ca54fe5ba1cbec59f3d5883fe191c"
    ),
    "002_create_rag_index.sql": (
        "e458d8238ea5c833982005130be61d61ed672296e0c2014aeaf9178d8fce25a2"
    ),
    "003_storage_guards.sql": (
        "08fa6ff5f6936dee68bc7d7e58e48786d1e42a0e427a9a7ef3725413a5bb6b6b"
    ),
    "004_staged_publication.sql": (
        "4f373bd275d8ab928b833546aa7be183eb5d0ba33b2e8f110a96ed2495e6f13c"
    ),
    "005_current_lexical_document.sql": (
        "c05a1a27301f5ab4bcd084092d1d3219a6f5b77a0f6e1ba3a62bf52218042f0c"
    ),
    "006_enrichment_lifecycle.sql": (
        "ce3631be5287f4a5d2d077b65686f50be09dd0f8393b831fa086d2e2c18ef5ca"
    ),
    "007_source_references.sql": (
        "1ad7fb7ab49b6c1b4cf09d85938ab43ade08cab4c10ad5e73e66da202f6ddf9e"
    ),
}


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
    project = Path(__file__).parents[4]
    uv = shutil.which("uv")
    assert uv is not None
    _ = subprocess.run(  # noqa: S603
        [uv, "build", "--wheel", "--out-dir", str(tmp_path), str(project)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("eventlens-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        scripts = sorted(
            name
            for name in archive.namelist()
            if name.startswith(PACKAGED_PREFIX) and name.endswith(".sql")
        )
    assert len(scripts) == 7
    # When: run packaged migration against fresh isolated schema outside checkout.
    schema = "wheel_" + uuid4().hex
    with psycopg.connect(database_url) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    scoped_url = make_conninfo(database_url, options=f"-c search_path={schema},public")
    probe = "from backend.rag.infra.postgres.migrations import migrate; "
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
    assert count == (7,)


def test_bundled_sql_preserves_original_migration_bytes() -> None:
    # Given: the single migration source and the pre-refactor checksums that
    # existing ledgers recorded (sha256 of each script's text).
    bundled = Path(__file__).parents[4] / MIGRATION_SOURCE
    # When/Then: same file set and byte-for-byte identity keep checksums stable.
    names = sorted(path.name for path in bundled.glob("*.sql"))
    assert tuple(names) == EXPECTED_MIGRATIONS
    assert {
        name: sha256((bundled / name).read_bytes()).hexdigest() for name in names
    } == RECORDED_CHECKSUMS


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
    source = Path(__file__).parents[4] / MIGRATION_SOURCE
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


def test_alembic_baseline_records_the_pinned_ledger_checksums() -> None:
    assert dict(LEGACY_CHECKSUMS) == RECORDED_CHECKSUMS


ALEMBIC_RESOURCES = (
    "backend/rag/infra/postgres/revisions/env.py",
    "backend/rag/infra/postgres/revisions/script.py.mako",
    "backend/rag/infra/postgres/revisions/versions/rag_0001_baseline.py",
    "backend/rag/infra/postgres/expected_schema.json",
)


def test_wheel_runs_alembic_migrate_outside_checkout(
    database_url: str, tmp_path: Path
) -> None:
    # Given: a built wheel unpacked outside the checkout (Alembic loads its
    # revisions from the filesystem, as in an installed environment).
    project = Path(__file__).parents[4]
    uv = shutil.which("uv")
    assert uv is not None
    _ = subprocess.run(  # noqa: S603
        [uv, "build", "--wheel", "--out-dir", str(tmp_path / "dist"), str(project)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next((tmp_path / "dist").glob("eventlens-*.whl"))
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        archive.extractall(site)
    assert set(ALEMBIC_RESOURCES) <= names
    schema = "wheel_alembic_" + uuid4().hex
    with psycopg.connect(database_url) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    scoped_url = make_conninfo(database_url, options=f"-c search_path={schema},public")
    probe = (
        "import backend, sys; assert backend.__file__.startswith(sys.argv[1]); "
        "from backend.rag.cli_main import app; "
        "app(['migrate'])"
    )

    # When: the packaged CLI migrates twice.
    runs = [
        subprocess.run(  # noqa: S603
            [sys.executable, "-c", probe, str(site)],
            cwd=tmp_path,
            env={
                **os.environ,
                "PYTHONPATH": str(site),
                "EVENTLENS_RAG_DATABASE_URL": scoped_url,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        for _ in range(2)
    ]

    # Then: baseline applied from packaged revisions, then a no-op.
    assert [run.returncode for run in runs] == [0, 0], runs[0].stderr
    assert all("alembic rag_0001_baseline" in run.stdout for run in runs)
    with psycopg.connect(scoped_url) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall()
        ledger = connection.execute(
            "SELECT count(*) FROM rag_schema_migrations"
        ).fetchone()
    assert version == [("rag_0001_baseline",)]
    assert ledger == (7,)
