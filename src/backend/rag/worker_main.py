"""rag-worker entry point: ``eventlens-rag-worker <command>``.

Commands: ``ingest``, ``enrich``, ``sync-labels``, ``summarize``, ``runs``,
``serve``.

Adapters come from ``backend.rag.composition``; this module only parses
arguments, maps typed failures to exit codes and prints JSON lines.
"""

import json
import os
import signal
import socket
import threading
import time
from collections import Counter
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import FrameType
from typing import Annotated
from uuid import UUID

import typer
from pydantic import ValidationError

from backend.rag.application.use_cases.enrichment import bundled_retry_policy
from backend.rag.composition import (
    FIXTURE_CALLER,
    NewsConfigurationError,
    Provider,
    build_label_sync,
    build_run_executor,
    build_runtime,
    build_summary_runner,
    json_lines_telemetry,
    prepare_storage,
    summary_provider_configured,
)
from backend.rag.infra.postgres.alembic_runner import LegacyAdoptionRequiredError
from backend.rag.infra.postgres.connection import (
    StorageOperationError,
    StorageUnavailableError,
)
from backend.rag.infra.postgres.engine import rag_engine
from backend.rag.infra.postgres.migrations import (
    LegacySchemaError,
)
from backend.rag.infra.postgres.work_queue import requeue_summary
from backend.rag.worker_pools import PoolWorker, dead_workers
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
STORAGE_FAILED_EXIT = 4
CONFIG_EXIT = 5
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


def _storage_failed(error: StorageOperationError) -> typer.Exit:
    """Report a rolled-back statement without SQL, values or traceback."""
    typer.echo(f"{error.code}: {error}", err=True)
    return typer.Exit(STORAGE_FAILED_EXIT)


def _prepare(database_url: str) -> None:
    """Apply pending Alembic revisions (idempotent) or exit typed; never stamps."""
    try:
        prepare_storage(database_url)
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    except StorageOperationError as error:
        raise _storage_failed(error) from error
    except (LegacyAdoptionRequiredError, LegacySchemaError) as error:
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
    except StorageOperationError as error:
        raise _storage_failed(error) from error
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
    except StorageOperationError as error:
        raise _storage_failed(error) from error
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


# --- news backend jobs (rag_work_jobs) --------------------------------------

WorkerId = Annotated[str, typer.Option("--worker-id")]


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def _configured[T](build: Callable[[], T]) -> T:
    try:
        return build()
    except NewsConfigurationError as error:
        typer.echo(f"rag_worker_not_configured: {error}", err=True)
        raise typer.Exit(CONFIG_EXIT) from error


def _guarded[T](work: Callable[[], T]) -> T:
    try:
        return work()
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    except StorageOperationError as error:
        raise _storage_failed(error) from error


@app.command("sync-labels")
def sync_labels_command(
    database_url: DatabaseUrl,
    limit: Annotated[int, typer.Option("--limit", min=1, max=5000)] = 500,
) -> None:
    """Project trusted enrichment publications into catalog labels."""
    _prepare(database_url)
    sync = build_label_sync(database_url, os.environ)
    report = _guarded(lambda: sync.execute(limit))
    typer.echo(json.dumps({"event": "label_sync"} | report.as_dict()))


@app.command("summary-requeue")
def summary_requeue_command(
    database_url: DatabaseUrl,
    article_id: Annotated[str, typer.Option("--article-id")],
) -> None:
    """Admin: retry the current revision's summary job (no model call here)."""
    _prepare(database_url)
    try:
        target = UUID(article_id)
    except ValueError as error:
        typer.echo("error:invalid_article_id", err=True)
        raise typer.Exit(2) from error
    status = _guarded(lambda: requeue_summary(rag_engine(database_url), target))
    typer.echo(json.dumps({"event": "summary_requeue", "status": status}))


@app.command("summarize")
def summarize_command(
    database_url: DatabaseUrl,
    max_jobs: Annotated[int, typer.Option("--max-jobs", min=1, max=25)] = 5,
    worker_id: WorkerId = "",
) -> None:
    """Summarize at most --max-jobs current revisions (needs a provider)."""
    _prepare(database_url)
    runner = _configured(lambda: build_summary_runner(database_url, os.environ))
    report = _guarded(lambda: runner.run(max_jobs, worker_id or _default_worker_id()))
    counts = Counter(outcome.status for outcome in report.outcomes)
    typer.echo(
        json.dumps(
            {
                "event": "summary_pass",
                "processed": len(report.outcomes),
                "outcomes": dict(sorted(counts.items())),
            }
        )
    )


@app.command("runs")
def runs_command(
    database_url: DatabaseUrl,
    max_jobs: Annotated[int, typer.Option("--max-jobs", min=1, max=25)] = 5,
    worker_id: WorkerId = "",
) -> None:
    """Execute at most --max-jobs queued conversation runs."""
    _prepare(database_url)
    executor = _configured(lambda: build_run_executor(database_url, os.environ))
    name = worker_id or _default_worker_id()
    results: list[str] = []
    while len(results) < max_jobs:
        claimed = _guarded(lambda: executor.claim(name, 1))
        if not claimed:
            break
        results.append(_guarded(partial(executor.execute, claimed[0])).status)
    typer.echo(
        json.dumps(
            {
                "event": "run_pass",
                "processed": len(results),
                "outcomes": dict(sorted(Counter(results).items())),
            }
        )
    )


@app.command("serve")
def serve_command(  # noqa: C901, PLR0913, PLR0915, PLR0917 - pool loop
    database_url: DatabaseUrl,
    query_workers: Annotated[int, typer.Option("--query-workers", min=0, max=8)] = 2,
    summary_workers: Annotated[
        int, typer.Option("--summary-workers", min=0, max=4)
    ] = 1,
    poll_seconds: Annotated[float, typer.Option("--poll-seconds", min=0.1)] = 1.0,
    label_sync_seconds: Annotated[
        float, typer.Option("--label-sync-seconds", min=1.0)
    ] = 30.0,
    idle_exit_seconds: Annotated[
        float, typer.Option("--idle-exit-seconds", min=0.0)
    ] = 0.0,
) -> None:
    """Long-running worker: separate pools for chat runs and summaries.

    Chat runs never wait behind summaries (separate claims and threads).
    Without a summary provider the summary pool is not started and jobs stay
    pending. Step errors (including typed storage errors) are logged, backed
    off and retried; a worker thread that still dies makes the process exit 1
    so the process manager restarts it. SIGINT/SIGTERM stops claiming;
    in-flight jobs finish or their leases expire and another worker re-claims
    them.
    """
    _prepare(database_url)
    executor = _configured(lambda: build_run_executor(database_url, os.environ))
    summaries = (
        _configured(lambda: build_summary_runner(database_url, os.environ))
        if summary_workers and summary_provider_configured(os.environ)
        else None
    )
    labels = build_label_sync(database_url, os.environ)
    stop = threading.Event()
    activity = [time.monotonic()]

    def on_signal(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        _ = signal.signal(signum, on_signal)

    def log(event: dict[str, object]) -> None:
        typer.echo(json.dumps(event, default=str))

    def touch() -> None:
        activity[0] = time.monotonic()

    def run_step(name: str) -> Callable[[], bool]:
        def step() -> bool:
            claimed = executor.claim(name, 1)
            if not claimed:
                return False
            result = executor.execute(claimed[0])
            log(
                {
                    "event": "run",
                    "run_id": result.run_id,
                    "status": result.status,
                    "code": result.code,
                }
            )
            return True

        return step

    def summary_step(name: str) -> Callable[[], bool]:
        def step() -> bool:
            assert summaries is not None  # noqa: S101
            report = summaries.run(1, name)
            for outcome in report.outcomes:
                log(
                    {
                        "event": "summary",
                        "article_id": outcome.article_id,
                        "status": outcome.status,
                        "code": outcome.code,
                    }
                )
            return bool(report.outcomes)

        return step

    def label_step() -> bool:
        counts: dict[str, object] = dict(labels.execute().as_dict())
        log({"event": "label_sync"} | counts)
        return False

    base = _default_worker_id()
    workers = [
        PoolWorker(
            f"{base}-q{i}", run_step(f"{base}-q{i}"), stop, log, poll_seconds, touch
        )
        for i in range(query_workers)
    ]
    if summaries is not None:
        workers += [
            PoolWorker(
                f"{base}-s{i}",
                summary_step(f"{base}-s{i}"),
                stop,
                log,
                poll_seconds,
                touch,
            )
            for i in range(summary_workers)
        ]
    workers.append(
        PoolWorker(f"{base}-labels", label_step, stop, log, label_sync_seconds)
    )
    for worker in workers:
        worker.start()
    log(
        {
            "event": "worker_started",
            "query_workers": query_workers,
            "summary_workers": 0 if summaries is None else summary_workers,
        }
    )
    exit_code = 0
    while not stop.is_set():
        dead = dead_workers(workers)
        if dead:  # a thread ended despite the supervised loop: restart the process
            log({"event": "worker_thread_died", "workers": list(dead)})
            exit_code = 1
            stop.set()
            break
        if idle_exit_seconds and time.monotonic() - activity[0] > idle_exit_seconds:
            stop.set()
        _ = stop.wait(poll_seconds)
    for worker in workers:
        if worker.thread is not None:
            worker.thread.join(timeout=30)
    log({"event": "worker_stopped", "exit_code": exit_code})
    if exit_code:
        raise typer.Exit(exit_code)
