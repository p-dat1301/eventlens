"""Typer adapter for worker use cases."""

import json
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from eventlens_rag.adapters.connection import StorageUnavailableError
from eventlens_rag.adapters.migrations import (
    LegacyDocumentError,
    LegacySchemaError,
    MigrationResourcesError,
)
from eventlens_rag.adapters.migrations import migrate as apply_migrations
from eventlens_rag.adapters.telemetry import JsonLinesTelemetry
from eventlens_rag.application.enrichment import bundled_retry_policy
from eventlens_worker.application import process_bundle_json, validate_bundle_json
from eventlens_worker.bundle import ArticleBundle
from eventlens_worker.enrichment import FIXTURE_CALLER, Provider, build_runtime
from eventlens_worker.fake_extractor import FakeEventExtractor
from eventlens_worker.memory_writer import InMemoryEventWriter
from eventlens_worker.runner import PipelineRunner, RunnerPorts

app = typer.Typer(no_args_is_help=True)

BundlePath = Annotated[
    Path,
    typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
]
FILE_READ_ERROR = "error:file_read_failed"
VALIDATION_ERROR = "error:bundle_validation_failed"


def _read_bundle(bundle_path: Path) -> str:
    """Read UTF-8 bundle text or map file errors to stable CLI output."""
    try:
        return bundle_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        typer.echo(FILE_READ_ERROR, err=True)
        raise typer.Exit(code=2) from error


@app.callback()
def worker() -> None:
    """Run EventLens worker commands."""


@app.command("validate")
def validate_command(bundle_path: BundlePath) -> None:
    """Validate one article bundle file."""
    try:
        result = validate_bundle_json(_read_bundle(bundle_path))
    except ValidationError as error:
        typer.echo(VALIDATION_ERROR, err=True)
        raise typer.Exit(code=2) from error
    typer.echo(
        f"Valid article {result.article_id} with {result.media_count} media items."
    )


@app.command("process")
def process_command(bundle_path: BundlePath) -> None:
    """Process one article bundle file through local adapters."""
    pipeline = PipelineRunner(
        ports=RunnerPorts(
            extractor=FakeEventExtractor(),
            writer=InMemoryEventWriter(),
        ),
        max_items=1,
    )
    try:
        trace = process_bundle_json(
            _read_bundle(bundle_path),
            pipeline,
        )
    except ValidationError as error:
        typer.echo(VALIDATION_ERROR, err=True)
        raise typer.Exit(code=2) from error
    typer.echo(trace.model_dump_json())


DatabaseUrl = Annotated[str, typer.Option(envvar="EVENTLENS_RAG_DATABASE_URL")]
UNAVAILABLE_EXIT = 3
MIGRATION_EXIT = 1
MAX_JOBS_PER_PASS = bundled_retry_policy().max_jobs_per_pass


def _unavailable(error: StorageUnavailableError) -> typer.Exit:
    """Report typed storage failure without leaking the connection string."""
    typer.echo(f"{error.code}: {error}", err=True)
    return typer.Exit(UNAVAILABLE_EXIT)


def _prepare(database_url: str) -> None:
    """Apply checksum-verified RAG migrations (idempotent) or exit typed."""
    try:
        apply_migrations(database_url)
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
        report = runtime.runner(JsonLinesTelemetry(typer.echo)).run(max_jobs)
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
