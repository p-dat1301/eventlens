import json
import os
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import scalar_row
from pydantic import TypeAdapter
from typer.testing import CliRunner

from eventlens_rag.adapters.fake_enricher import FakeArticleEnricher
from eventlens_worker.cli import app
from eventlens_worker.enrichment import Provider, build_runtime

ROOT = Path(__file__).parents[3]
BUNDLE = ROOT / "fixtures/article_bundle.v1.json"
UNREACHABLE = "postgresql://rag_test:hunter2-secret@127.0.0.1:1/rag_test"
VALIDATION_ERROR = "error:bundle_validation_failed\n"
RECORDS = TypeAdapter(dict[str, object])


@pytest.fixture
def scoped_url() -> Generator[str]:
    base = os.environ.get("EVENTLENS_RAG_DATABASE_URL")
    if base is None:
        pytest.skip("EVENTLENS_RAG_DATABASE_URL required for real PostgreSQL test")
    schema = "worker_t4_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    yield make_conninfo(base, options=f"-c search_path={schema},public")
    with psycopg.connect(base, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )


def invoke(args: list[str], url: str) -> tuple[int, list[dict[str, object]], str]:
    result = CliRunner().invoke(app, args, env={"EVENTLENS_RAG_DATABASE_URL": url})
    lines = [RECORDS.validate_json(line) for line in result.stdout.splitlines()]
    return result.exit_code, lines, result.stderr


def scalar(url: str, query: str) -> object:
    with (
        psycopg.connect(url) as connection,
        connection.cursor(row_factory=scalar_row) as cursor,
    ):
        _ = cursor.execute(query.encode())
        return cursor.fetchone()


def test_ingest_rejects_invalid_bundle_before_touching_storage(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    _ = invalid.write_text("{}", encoding="utf-8")
    code, _, stderr = invoke(["ingest", str(invalid)], UNREACHABLE)
    assert code == 2
    assert stderr == VALIDATION_ERROR


@pytest.mark.parametrize(
    "args", [["ingest", str(BUNDLE)], ["enrich", "--max-jobs", "1"]]
)
def test_unavailable_storage_exits_3_without_leaking_credentials(
    args: list[str],
) -> None:
    code, _, stderr = invoke(args, UNREACHABLE)
    assert code == 3
    assert stderr.startswith("rag_storage_unavailable:")
    assert "hunter2" not in stderr


@pytest.mark.parametrize(
    "args",
    [
        ["enrich", "--max-jobs", "0"],
        ["enrich", "--max-jobs", "26"],
        ["enrich", "--provider", "openai"],
    ],
)
def test_enrich_rejects_out_of_policy_options(args: list[str]) -> None:
    code, _, _ = invoke(args, UNREACHABLE)
    assert code == 2


def test_api_key_alone_never_activates_a_live_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENTLENS_OPENAI_API_KEY", "sk-should-not-be-used")
    runtime = build_runtime(UNREACHABLE)
    assert isinstance(runtime.steps.enricher, FakeArticleEnricher)
    assert {provider.value for provider in Provider} == {"fake", "fake-timeout"}


def test_cli_happy_flow_ingest_then_enrich_publishes(scoped_url: str) -> None:
    code, (first,), _ = invoke(["ingest", str(BUNDLE)], scoped_url)
    assert code == 0
    assert (first["event"], first["job_created"], first["current"]) == (
        "article_ingested",
        True,
        True,
    )
    code, (again,), _ = invoke(["ingest", str(BUNDLE)], scoped_url)
    assert (code, again["job_id"], again["job_created"]) == (
        0,
        first["job_id"],
        False,
    )
    lexical = """SELECT count(*) FROM rag_current_lexical
        WHERE search_vector @@ plainto_tsquery('simple', 'công')"""
    assert scalar(scoped_url, lexical) == 1
    code, lines, _ = invoke(["enrich", "--max-jobs", "5"], scoped_url)
    assert code == 0
    job, summary = lines
    assert (job["event"], job["outcome"], job["job_id"]) == (
        "enrichment_job",
        "READY",
        first["job_id"],
    )
    assert summary == {
        "event": "enrichment_pass",
        "policy_id": "enrichment-retry.v1",
        "reconciled": 0,
        "processed": 1,
        "outcomes": {"READY": 1},
    }
    assert "Thành phố" not in json.dumps(lines)
    assert scalar(scoped_url, "SELECT count(*) FROM rag_current_vectors") == 1
    code, lines, _ = invoke(["enrich"], scoped_url)
    assert (code, lines[-1]["processed"]) == (0, 0)


def test_cli_failure_flow_timeout_retries_then_fails_lexical_survives(
    scoped_url: str,
) -> None:
    assert invoke(["ingest", str(BUNDLE)], scoped_url)[0] == 0
    timeout = ["enrich", "--provider", "fake-timeout"]
    outcomes: list[object] = []
    for _ in range(3):
        code, lines, _ = invoke(timeout, scoped_url)
        assert code == 0
        outcomes.extend(line["outcome"] for line in lines[:-1])
        with psycopg.connect(scoped_url) as connection:
            _ = connection.execute(
                """UPDATE rag_enrichment_jobs
                SET next_attempt_at = clock_timestamp() - interval '1 second'"""
            )
    assert outcomes == ["RETRY_SCHEDULED", "RETRY_SCHEDULED", "FAILED"]
    status = "SELECT status || ':' || failure_code FROM rag_enrichment_jobs"
    assert scalar(scoped_url, status) == "FAILED:provider_timeout"
    lexical = """SELECT count(*) FROM rag_current_lexical
        WHERE search_vector @@ plainto_tsquery('simple', 'công')"""
    assert scalar(scoped_url, lexical) == 1
    code, lines, _ = invoke(["enrich"], scoped_url)
    assert (code, lines[-1]["processed"]) == (0, 0)
