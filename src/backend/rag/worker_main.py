"""rag-worker entry point: ``eventlens-rag-worker ingest|enrich``.

Adapters come from ``backend.rag.composition``; this module only parses
arguments, maps typed failures to exit codes and prints JSON lines.
"""

import json
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from backend.rag.application.use_cases.enrichment import bundled_retry_policy
from backend.rag.composition import (
    FIXTURE_CALLER,
    Provider,
    build_runtime,
    json_lines_telemetry,
    prepare_storage,
)
from backend.rag.infra.postgres.connection import StorageUnavailableError
from backend.rag.infra.postgres.migrations import (
    LegacyDocumentError,
    LegacySchemaError,
    MigrationResourcesError,
)
from backend.shared.contracts.bundle import ArticleBundle

app = typer.Typer(no_args_is_help=True)

BundlePath = Annotated[
    Path,
    typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
]
FILE_READ_ERROR = "error:file_read_failed"
VALIDATION_ERROR = "error:bundle_validation_failed"
DatabaseUrl = Annotated[str, typer.Option(envvar="EVENTLENS_RAG_DATABASE_URL")]
UNAVAILABLE_EXIT = 3
MIGRATION_EXIT = 1
MAX_JOBS_PER_PASS = bundled_retry_policy().max_jobs_per_pass


def _read_bundle(bundle_path: Path) -> str:
    """Read UTF-8 bundle text or map file errors to stable CLI output."""
    try:
        return bundle_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        typer.echo(FILE_READ_ERROR, err=True)
        raise typer.Exit(code=2) from error


@app.callback()
def worker() -> None:
    """Run EventLens RAG worker commands."""


def _unavailable(error: StorageUnavailableError) -> typer.Exit:
    """Report typed storage failure without leaking the connection string."""
    typer.echo(f"{error.code}: {error}", err=True)
    return typer.Exit(UNAVAILABLE_EXIT)


def _prepare(database_url: str) -> None:
    """Apply checksum-verified RAG migrations (idempotent) or exit typed."""
    try:
        prepare_storage(database_url)
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    except (LegacyDocumentError, LegacySchemaError, MigrationResourcesError) as error:
        typer.echo(f"rag_migration_failed: {error}", err=True)
        raise typer.Exit(MIGRATION_EXIT) from error


@app.command("ingest")
def ingest_command(bundle_path: BundlePath, database_url: DatabaseUrl) -> None:
    """Persist one bundle's article (lexical now) and enqueue enrichment."""
    try:
        bundle = ArticleBundle.model_validate_json(_read_bundle(bundle_path))
    except ValidationError as error:
        typer.echo(VALIDATION_ERROR, err=True)
        raise typer.Exit(code=2) from error
    _prepare(database_url)
    try:
        receipt = build_runtime(database_url).ingest.execute(
            bundle.article, FIXTURE_CALLER
        )
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    typer.echo(
        json.dumps(
            {
                "event": "article_ingested",
                "article_id": str(receipt.article_id),
                "content_hash": receipt.content_hash,
                "job_id": None if receipt.job_id is None else str(receipt.job_id),
                "job_created": receipt.job_created,
                "current": receipt.current,
            }
        )
    )


@app.command("enrich")
def enrich_command(
    database_url: DatabaseUrl,
    max_jobs: Annotated[
        int, typer.Option("--max-jobs", min=1, max=MAX_JOBS_PER_PASS)
    ] = MAX_JOBS_PER_PASS,
    provider: Annotated[Provider, typer.Option("--provider")] = Provider.FAKE,
) -> None:
    """Reconcile, then process at most --max-jobs eligible enrichment jobs."""
    _prepare(database_url)
    runtime = build_runtime(database_url, provider)
    try:
        report = runtime.runner(json_lines_telemetry(typer.echo)).run(max_jobs)
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    counts = Counter(outcome.kind.value for outcome in report.outcomes)
    typer.echo(
        json.dumps(
            {
                "event": "enrichment_pass",
                "policy_id": runtime.policy.policy_id,
                "reconciled": report.reconciled,
                "processed": len(report.outcomes),
                "outcomes": dict(sorted(counts.items())),
            }
        )
    )
