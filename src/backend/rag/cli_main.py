"""``eventlens-rag`` entry point: local RAG database maintenance and smokes.

Outer-ring entry point (like ``api_main``/``worker_main``): it drives
PostgreSQL adapters and diagnostics harnesses directly, so it lives beside the
composition root rather than in ``presentation``.
"""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer
from pydantic import TypeAdapter

from backend.rag.application.use_cases.index import EnsureGeneration
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings
from backend.rag.infra.postgres.alembic_runner import (
    LegacyAdoptionRequiredError,
    PreflightFailedError,
    PreflightReport,
    adopt_legacy,
)
from backend.rag.infra.postgres.alembic_runner import (
    preflight as run_preflight,
)
from backend.rag.infra.postgres.alembic_runner import (
    upgrade_head as apply_migrations,
)
from backend.rag.infra.postgres.connection import (
    StorageOperationError,
    StorageUnavailableError,
)
from backend.rag.infra.postgres.engine import (
    create_rag_engine,
    execute,
    rag_engine,
    scalars,
    transaction,
)
from backend.rag.infra.postgres.migrations import LegacySchemaError
from backend.rag.infra.postgres.staging import PostgresStaging
from backend.rag.infra.postgres.store import PostgresRagStore

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from uuid import UUID

    from backend.rag.application.handoff_validation import HandoffReport
    from backend.rag.application.ports.retrieval import RetrievalStore
    from backend.rag.composition import SearchRuntime
    from backend.rag.evaluation.metrics import Json
    from backend.rag.evaluation.search_eval.dataset import EvalQuestion, SearchDataset
    from backend.rag.evaluation.search_eval.factory import SystemOptions
    from backend.rag.evaluation.search_eval.scoring import RankedPassage
    from backend.rag.evaluation.search_eval.systems import SearchSystem, SystemAnswer

    TruthFactory = Callable[[Sequence[ArticleDocument]], RetrievalStore]

app = typer.Typer(no_args_is_help=True)
ROOT = Path(__file__).parents[3]
FIXTURE_ARTICLES = ROOT / "fixtures/rag/articles"
UNAVAILABLE_EXIT = 3
STORAGE_FAILED_EXIT = 4
SMOKE_FAILED_EXIT = 2
EVAL_OUTPUT = ROOT / ".omo/evidence/T9"

DatabaseUrl = Annotated[str, typer.Option(envvar="EVENTLENS_RAG_DATABASE_URL")]
MIGRATION_ERRORS = (LegacyAdoptionRequiredError, LegacySchemaError)


def _unavailable(error: StorageUnavailableError) -> typer.Exit:
    """Report typed storage failure without leaking the connection string."""
    typer.echo(f"{error.code}: {error}", err=True)
    return typer.Exit(UNAVAILABLE_EXIT)


def _storage_failed(error: StorageOperationError) -> typer.Exit:
    """Report a rolled-back statement without SQL, values or traceback."""
    typer.echo(f"{error.code}: {error}", err=True)
    return typer.Exit(STORAGE_FAILED_EXIT)


def _migration_failed(error: Exception) -> typer.Exit:
    typer.echo(f"rag_migration_failed: {error}", err=True)
    return typer.Exit(1)


def _report(report: PreflightReport) -> str:
    return json.dumps(
        {
            "ok": report.ok,
            "adoptable": report.adoptable,
            "alembic_revision": report.alembic_revision,
            "problems": list(report.problems),
            "row_counts": dict(report.row_counts),
        },
        indent=2,
        sort_keys=True,
    )


@app.command()
def migrate(database_url: DatabaseUrl) -> None:
    """Apply pending Alembic revisions (the only RAG migration authority)."""
    try:
        revision = apply_migrations(database_url)
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    except StorageOperationError as error:
        raise _storage_failed(error) from error
    except MIGRATION_ERRORS as error:
        raise _migration_failed(error) from error
    typer.echo(f"rag migrations applied (alembic {revision})")


@app.command()
def preflight(database_url: DatabaseUrl) -> None:
    """Read-only check that a historical 001-007 database may be adopted."""
    try:
        report = run_preflight(database_url)
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    except StorageOperationError as error:
        raise _storage_failed(error) from error
    typer.echo(_report(report))
    if not report.ok:
        raise typer.Exit(1)


@app.command("adopt-legacy")
def adopt(database_url: DatabaseUrl) -> None:
    """Verify a historical 001-007 database, then stamp the Alembic baseline.

    Back up the database and stop writers first. No historical DDL is re-run.
    """
    try:
        report = adopt_legacy(database_url)
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    except StorageOperationError as error:
        raise _storage_failed(error) from error
    except PreflightFailedError as error:
        typer.echo(f"rag_adoption_refused: {error}", err=True)
        raise typer.Exit(1) from error
    typer.echo(_report(report))
    typer.echo(f"rag database managed by alembic at {report.alembic_revision}")


@app.command("load-fixtures")
def load_fixtures(
    database_url: DatabaseUrl,
    directory: Annotated[Path, typer.Option()] = FIXTURE_ARTICLES,
) -> None:
    """Persist fixture ArticleDocuments; lexical search is available immediately."""
    if not directory.is_dir():
        typer.echo(f"rag_fixture_missing: {directory}", err=True)
        raise typer.Exit(1)
    paths = sorted(directory.glob("*.json"))
    engine = create_rag_engine(database_url)
    try:
        store = PostgresRagStore(engine)
        for path in paths:
            article = ArticleDocument.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            store.persist_article(article)
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    except StorageOperationError as error:
        raise _storage_failed(error) from error
    finally:
        engine.dispose()
    typer.echo(f"rag persisted {len(paths)} fixture articles")


@app.command()
def rebuild(database_url: DatabaseUrl) -> None:
    """Stage every current article with fake embeddings, then flip atomically."""
    engine = create_rag_engine(database_url)
    try:
        _ = apply_migrations(database_url)
        with transaction(engine, read_only=True) as connection:
            documents = TypeAdapter(list[str]).validate_python(
                scalars(
                    execute(
                        connection,
                        "SELECT document_json::text FROM rag_current_lexical",
                    )
                )
            )
        staging = PostgresStaging(engine)
        ensure = EnsureGeneration(staging, FakeEmbeddings(), EmbeddingIdentity())
        staged = [
            ensure.execute(ArticleDocument.model_validate_json(document))
            for document in documents
        ]
        published = staging.activate_all([item.generation for item in staged])
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    except StorageOperationError as error:
        raise _storage_failed(error) from error
    except MIGRATION_ERRORS as error:
        raise _migration_failed(error) from error
    finally:
        engine.dispose()
    reused = sum(item.reused for item in staged)
    summary = f"{published} current articles ({reused} reused generations)"
    typer.echo(f"rag fake rebuild published {summary}")


@app.command("retrieval-smoke")
def retrieval_smoke(
    database_url: DatabaseUrl,
    output: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """T5 smoke on an EMPTY isolated DB: cases + fixture mode measurement."""
    from backend.rag.infra.diagnostics.retrieval_smoke import (  # noqa: PLC0415
        SmokeDatabaseNotEmptyError,
        dump,
        run_smoke,
    )

    try:
        report = run_smoke(database_url, ROOT / "fixtures/rag")
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    except StorageOperationError as error:
        raise _storage_failed(error) from error
    except MIGRATION_ERRORS as error:
        raise _migration_failed(error) from error
    except SmokeDatabaseNotEmptyError as error:
        typer.echo(f"rag_smoke_refused: {error}", err=True)
        raise typer.Exit(1) from error
    text = dump(report)
    if output is not None:
        _ = output.write_text(text + "\n", encoding="utf-8")
    typer.echo(text)


@app.command("synthesis-smoke")
def synthesis_smoke(
    database_url: DatabaseUrl,
    output: Annotated[Path | None, typer.Option()] = None,
    evidence: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """T6 smoke on an EMPTY isolated DB: T5 retrieval -> T6 fake synthesis."""
    from backend.rag.infra.diagnostics.retrieval_smoke import (  # noqa: PLC0415
        SmokeDatabaseNotEmptyError,
    )
    from backend.rag.infra.diagnostics.synthesis_smoke import (  # noqa: PLC0415
        dump,
        run_smoke,
    )

    try:
        report, excerpts = run_smoke(database_url, ROOT / "fixtures/rag")
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    except StorageOperationError as error:
        raise _storage_failed(error) from error
    except MIGRATION_ERRORS as error:
        raise _migration_failed(error) from error
    except SmokeDatabaseNotEmptyError as error:
        typer.echo(f"rag_smoke_refused: {error}", err=True)
        raise typer.Exit(1) from error
    text = dump(report)
    if output is not None:
        _ = output.write_text(text + "\n", encoding="utf-8")
    if evidence is not None:
        _ = evidence.write_text(dump(excerpts) + "\n", encoding="utf-8")
    typer.echo(text)
    if report.get("all_passed") is not True:
        typer.echo("synthesis_smoke_failed: an expected case did not hold", err=True)
        raise typer.Exit(SMOKE_FAILED_EXIT)


@app.command("vector-route-report")
def vector_route_report(
    database_url: DatabaseUrl,
    output: Annotated[Path, typer.Option()],
    articles: Annotated[int, typer.Option()] = 3000,
    queries: Annotated[int, typer.Option()] = 20,
    k: Annotated[int, typer.Option()] = 10,
) -> None:
    """Exact vs HNSW ANN on a synthetic corpus in an EMPTY isolated DB."""
    from backend.rag.infra.diagnostics.vector_routes import (  # noqa: PLC0415
        CorpusNotEmptyError,
        load_corpus,
        measure,
        pgvector_version,
        report_rows,
    )

    output.mkdir(parents=True, exist_ok=True)
    try:
        _ = apply_migrations(database_url)
        load_corpus(database_url, articles, seed=5)
        results = measure(database_url, output, queries=queries, k=k, seed=5)
        version = pgvector_version(database_url)
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    except StorageOperationError as error:
        raise _storage_failed(error) from error
    except MIGRATION_ERRORS as error:
        raise _migration_failed(error) from error
    except CorpusNotEmptyError as error:
        typer.echo(f"rag_report_refused: {error}", err=True)
        raise typer.Exit(1) from error
    report = {
        "pgvector_version": version,
        "articles": articles,
        "queries": queries,
        "k": k,
        "production_route": "exact (retrieval-policy.v3, ann_enabled=false)",
        "ann_settings": {
            "hnsw.ef_search": 40,
            "hnsw.iterative_scan": "relaxed_order",
            "hnsw.max_scan_tuples": 20000,
        },
        "results": report_rows(results),
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    _ = (output / "vector-route-report.json").write_text(text + "\n", encoding="utf-8")
    typer.echo(text)


def _write_eval(name: str, output: Path, report: tuple[object, object]) -> None:
    """Write deterministic + measured JSON; exit 2 if a gating case failed."""
    from backend.rag.evaluation.gating import gate_failures  # noqa: PLC0415
    from backend.rag.evaluation.metrics import Json, canonical  # noqa: PLC0415

    deterministic, measured = TypeAdapter(tuple[dict[str, Json], Json]).validate_python(
        report
    )
    output.mkdir(parents=True, exist_ok=True)
    _ = (output / f"{name}.json").write_text(
        canonical(deterministic) + "\n", encoding="utf-8"
    )
    _ = (output / f"{name}.measured.json").write_text(
        canonical(measured) + "\n", encoding="utf-8"
    )
    typer.echo(f"{name}: report {output / f'{name}.json'}")
    typer.echo(f"{name}: digest {deterministic['report_digest']}")
    failures = gate_failures(deterministic)
    for failure in failures:
        typer.echo(f"{name}: gating check failed: {failure}", err=True)
    if failures:
        raise typer.Exit(SMOKE_FAILED_EXIT)
    typer.echo(f"{name}: gating checks held (quality metrics are reported, not gated)")


@app.command("enrichment-eval")
def enrichment_eval(output: Annotated[Path, typer.Option()] = EVAL_OUTPUT) -> None:
    """T9 enrichment report: fake enricher vs manual gold (no DB, no network)."""
    from backend.rag.evaluation.corpus import load_corpus  # noqa: PLC0415
    from backend.rag.evaluation.enrichment_eval import evaluate  # noqa: PLC0415

    _write_eval("enrichment-eval", output, evaluate(load_corpus()))


@app.command("rag-eval")
def rag_eval(
    output: Annotated[Path, typer.Option()] = EVAL_OUTPUT,
    repeats: Annotated[int, typer.Option(min=1, max=20)] = 5,
) -> None:
    """T9 RAG report: lexical vs hybrid on one compiled graph (no DB, no network)."""
    from backend.rag.evaluation.corpus import load_corpus  # noqa: PLC0415
    from backend.rag.evaluation.rag_eval import evaluate  # noqa: PLC0415

    _write_eval("rag-eval", output, evaluate(load_corpus(), repeats))


@app.command("db-test")
def db_test() -> None:
    """Run isolated real PostgreSQL suite using an owned ephemeral container."""
    completed = subprocess.run(
        ["/bin/sh", "scripts/rag/db-test.sh"],
        cwd=ROOT,
        check=False,
    )
    sys.exit(completed.returncode)


# --- search platform: derived index maintenance ------------------------------

index_app = typer.Typer(
    no_args_is_help=True,
    help="Derived search index (Elasticsearch) maintenance; PostgreSQL is the truth.",
)
app.add_typer(index_app, name="index")


def _search_runtime(database_url: str, embeddings: str | None) -> "SearchRuntime":
    from backend.rag.composition import (  # noqa: PLC0415
        SearchConfigurationError,
        SearchSettings,
        build_search_runtime,
    )

    environ = dict(os.environ)
    if embeddings is not None:
        environ["EVENTLENS_RAG_SEARCH_EMBEDDINGS"] = embeddings
    _ = environ.setdefault("EVENTLENS_RAG_SEARCH_BACKEND", "elasticsearch")
    try:
        settings = SearchSettings.from_environment(environ)
    except SearchConfigurationError as error:
        typer.echo(f"search_configuration_invalid: {error}", err=True)
        raise typer.Exit(2) from None
    return build_search_runtime(database_url, settings)


def _search_failure(error: Exception) -> typer.Exit:
    """Typed exit: 3 unreachable (index or DB), 4 refused/failed."""
    from backend.rag.application.ports.search_index import (  # noqa: PLC0415
        SearchIndexUnavailableError,
    )

    code = getattr(error, "code", type(error).__name__)
    typer.echo(f"{code}: {error}", err=True)
    if isinstance(error, SearchIndexUnavailableError | StorageUnavailableError):
        return typer.Exit(UNAVAILABLE_EXIT)
    return typer.Exit(STORAGE_FAILED_EXIT)


def _emit(payload: dict[str, object]) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


Embeddings = Annotated[
    str | None,
    typer.Option(help="none | fake-bge-m3 | bge-m3 (overrides the environment)"),
]


@index_app.command("rebuild")
def index_rebuild(
    database_url: DatabaseUrl,
    embeddings: Embeddings = None,
    drop_previous: Annotated[bool, typer.Option()] = False,  # noqa: FBT002
) -> None:
    """Build a new generation from PostgreSQL and activate it when complete."""
    from backend.rag.application.ports.search_index import (  # noqa: PLC0415
        SearchIndexError,
    )
    from backend.rag.domain.search_embedding import (  # noqa: PLC0415
        EmbeddingDimensionMismatchError,
        EmbeddingIdentityMismatchError,
    )

    runtime = _search_runtime(database_url, embeddings)
    runtime.rebuild.drop_previous = drop_previous
    identity = None if runtime.embedder is None else runtime.embedder.identity
    try:
        report = runtime.rebuild.execute(identity)
    except (
        SearchIndexError,
        StorageUnavailableError,
        StorageOperationError,
        EmbeddingDimensionMismatchError,
        EmbeddingIdentityMismatchError,
    ) as error:
        raise _search_failure(error) from None
    _emit(
        {
            "generation": report.generation.label,
            "index": report.generation.index_name,
            "previous": None if report.previous is None else report.previous.label,
            "revisions": report.revisions,
            "chunks": report.chunks,
            "indexed": report.indexed,
            "embedded": report.embedded,
            "cache_hits": report.cache_hits,
            "latency_ms": round(report.latency_ms, 1),
        }
    )


@index_app.command("reconcile")
def index_reconcile(
    database_url: DatabaseUrl,
    repair: Annotated[bool, typer.Option()] = False,  # noqa: FBT002
    deep: Annotated[bool, typer.Option(help="also compare chunk counts")] = False,  # noqa: FBT002
) -> None:
    """Compare PostgreSQL current revisions with the index (exit 1 on drift)."""
    from backend.rag.application.ports.search_index import (  # noqa: PLC0415
        SearchIndexError,
    )

    runtime = _search_runtime(database_url, None)
    try:
        report = runtime.reconcile.execute(repair=repair, deep=deep)
    except (SearchIndexError, StorageUnavailableError, StorageOperationError) as error:
        raise _search_failure(error) from None
    _emit(
        {
            "generation": report.generation.label,
            "postgres_revisions": report.postgres_revisions,
            "index_revisions": report.index_revisions,
            "missing": [str(a) for a in report.missing],
            "stale": [str(a) for a in report.stale],
            "extra": [str(a) for a in report.extra],
            "chunk_mismatch": [str(a) for a in report.chunk_mismatch],
            "repaired": report.repaired,
            "consistent": report.consistent,
        }
    )
    if not report.consistent and not repair:
        raise typer.Exit(1)


@index_app.command("health")
def index_health(database_url: DatabaseUrl) -> None:
    """Engine, ACTIVE generation and outbox state (exit 1 when unhealthy)."""
    runtime = _search_runtime(database_url, None)
    try:
        report = runtime.health.execute()
    except (StorageUnavailableError, StorageOperationError) as error:
        raise _search_failure(error) from None
    engine = report.engine
    _emit(
        {
            "healthy": report.healthy,
            "engine": engine.engine,
            "reachable": engine.reachable,
            "engine_version": engine.engine_version,
            "index_exists": engine.index_exists,
            "documents": engine.documents,
            "status": engine.status,
            "generation": None
            if report.generation is None
            else report.generation.label,
            "outbox": {status.value: count for status, count in report.outbox.items()},
            "oldest_pending_age_seconds": report.oldest_pending_age_seconds,
        }
    )
    if not report.healthy:
        raise typer.Exit(1)


@index_app.command("drain-outbox")
def index_drain_outbox(
    database_url: DatabaseUrl,
    max_batches: Annotated[int, typer.Option(min=1, max=1000)] = 10,
    requeue_failed: Annotated[bool, typer.Option()] = False,  # noqa: FBT002
) -> None:
    """Apply outbox events to the index (DONE only after the index acked)."""
    runtime = _search_runtime(database_url, None)
    try:
        requeued = runtime.outbox.requeue_failed() if requeue_failed else 0
        report = runtime.drain.execute(max_batches)
    except (StorageUnavailableError, StorageOperationError) as error:
        raise _search_failure(error) from None
    _emit(
        {
            "requeued": requeued,
            "claimed": report.claimed,
            "done": report.done,
            "retried": report.retried,
            "failed": report.failed,
        }
    )


# --- external handoff corpora (data-team batches) ------------------------------

PathOption = Annotated[Path | None, typer.Option(dir_okay=False, exists=True)]
HANDOFF_INVALID_EXIT = 2


def _require_eval(database_url: str) -> None:
    """Exit unless the database is an ``*_eval`` database (never dev/prod)."""
    from backend.rag.infra.postgres.eval_database import (  # noqa: PLC0415
        EvalDatabaseRefusedError,
        require_eval_database,
    )

    try:
        require_eval_database(database_url)
    except EvalDatabaseRefusedError as error:
        typer.echo(f"handoff_invalid: {error}", err=True)
        raise typer.Exit(HANDOFF_INVALID_EXIT) from None


def _table_counts(database_url: str) -> dict[str, int]:
    from backend.rag.infra.postgres.eval_database import (  # noqa: PLC0415
        table_counts,
    )

    return table_counts(database_url)


def _validated(articles: Path, manifest: Path) -> "HandoffReport":
    from backend.rag.application.handoff_validation import (  # noqa: PLC0415
        validate_handoff,
    )

    return validate_handoff(articles.read_bytes(), manifest.read_bytes())


@app.command("handoff-validate")
def handoff_validate(
    articles: Annotated[Path, typer.Option(dir_okay=False, exists=True)],
    manifest: Annotated[Path, typer.Option(dir_okay=False, exists=True)],
    output: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
) -> None:
    """Validate a handoff batch; writes a text-free report (IDs, codes, counts)."""
    report = _validated(articles, manifest)
    text = json.dumps(report.to_json(), ensure_ascii=False, indent=2, sort_keys=True)
    if output is None:
        typer.echo(text)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        _ = output.write_text(text + "\n", encoding="utf-8")
        typer.echo(f"handoff-validate: ok={report.ok} -> {output}")
    if not report.ok:
        raise typer.Exit(HANDOFF_INVALID_EXIT)


def _import(database_url: str, report: "HandoffReport") -> dict[str, object]:
    from datetime import UTC, datetime  # noqa: PLC0415

    from backend.rag.application.use_cases.import_handoff import (  # noqa: PLC0415
        ImportHandoff,
    )
    from backend.rag.composition import FIXTURE_CALLER  # noqa: PLC0415
    from backend.rag.infra.access.fixture_access import (  # noqa: PLC0415
        FixtureAccessPolicy,
    )
    from backend.rag.infra.postgres.source_versions import (  # noqa: PLC0415
        PostgresSourceVersions,
    )

    _require_eval(database_url)
    before = _table_counts(database_url)
    receipt = ImportHandoff(
        PostgresSourceVersions(rag_engine(database_url)),
        FixtureAccessPolicy(),
        lambda: datetime.now(UTC),
    ).execute(report, FIXTURE_CALLER)
    return {
        "export_id": receipt.export_id,
        "records": receipt.records,
        "revisions_inserted": receipt.revisions_inserted,
        "provenance_inserted": receipt.provenance_inserted,
        "current": receipt.current,
        "tables_before": before,
        "tables_after": _table_counts(database_url),
    }


@app.command("handoff-import")
def handoff_import(
    articles: Annotated[Path, typer.Option(dir_okay=False, exists=True)],
    manifest: Annotated[Path, typer.Option(dir_okay=False, exists=True)],
    database_url: Annotated[
        str, typer.Option(envvar="EVENTLENS_RAG_EVAL_DATABASE_URL")
    ],
) -> None:
    """Validate, then import the batch atomically into a ``*_eval`` database."""
    from backend.rag.application.ports.source_versions import (  # noqa: PLC0415
        ProvenanceConflictError,
    )
    from backend.rag.application.use_cases.import_handoff import (  # noqa: PLC0415
        HandoffRejectedError,
    )

    report = _validated(articles, manifest)
    try:
        receipt = _import(database_url, report)
    except (HandoffRejectedError, ProvenanceConflictError) as error:
        typer.echo(f"handoff_rejected: {error}", err=True)
        raise typer.Exit(HANDOFF_INVALID_EXIT) from None
    typer.echo(json.dumps(receipt, sort_keys=True))


# --- search-selection benchmark ----------------------------------------------

SEARCH_EVAL_OUTPUT = ROOT / ".omo/evidence/search-platform/reports"
REVIEW_OUTPUT = ROOT / ".private/search-review"


def _git_metadata() -> "dict[str, Json]":
    """Commit, dirty flag and a digest of the exact uncommitted tree.

    ``worktree_digest`` hashes ``git diff HEAD`` plus every untracked,
    non-ignored file (path and bytes), excluding evidence output under
    ``.omo/`` and ``.private/``, so an uncommitted run is identifiable.
    """

    def git(*args: str) -> bytes:
        completed = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        return completed.stdout

    excluded = (".omo/", ".private/")
    digest = hashlib.sha256(
        git("diff", "HEAD", "--binary", "--", ".", ":!.omo", ":!.private")
    )
    untracked = sorted(
        name
        for name in git("ls-files", "--others", "--exclude-standard")
        .decode()
        .splitlines()
        if not name.startswith(excluded)
    )
    for name in untracked:
        digest.update(name.encode() + b"\0")
        digest.update((ROOT / name).read_bytes())
    return {
        "git_commit": git("rev-parse", "HEAD").decode().strip(),
        "git_dirty": bool(git("status", "--porcelain").strip()),
        "worktree_digest": "sha256:" + digest.hexdigest(),
        "untracked_files": len(untracked),
    }


def _postgres_lexical(
    database_url: str | None, dataset: "SearchDataset | None" = None
) -> "Callable[[], SearchSystem] | None":
    """PostgreSQL FTS configuration over an isolated database.

    Fixture runs need an empty database and persist the corpus themselves; a
    handoff run has already imported the batch (with provenance) and only
    queries it, with the dataset's frozen query clock.
    """
    if not database_url:
        return None
    imported = dataset is not None and dataset.kind == "handoff"
    from backend.rag.evaluation.search_eval.systems import (  # noqa: PLC0415
        PostgresLexicalSystem,
    )
    from backend.rag.infra.retrieval.postgres_retrieval import (  # noqa: PLC0415
        PostgresRetrievalStore,
    )

    def build() -> "SearchSystem":
        engine = create_rag_engine(database_url)
        with transaction(engine) as connection:
            count = scalars(
                execute(connection, "SELECT count(*) FROM rag_article_documents")
            )
        if not imported and count and count[0]:
            typer.echo(
                "search_eval_invalid: database must be isolated and empty", err=True
            )
            raise typer.Exit(2)
        if imported:
            assert dataset is not None  # noqa: S101
            return PostgresLexicalSystem(
                PostgresRetrievalStore(engine),
                lambda _document: None,
                as_of=dataset.as_of,
            )
        return PostgresLexicalSystem(
            PostgresRetrievalStore(engine), PostgresRagStore(engine).persist_article
        )

    return build


def _pipeline_inputs(
    database_url: str | None, dataset: "SearchDataset | None"
) -> "tuple[datetime | None, TruthFactory | None]":
    """Query clock and final-check store for ``name#profile`` configurations.

    A handoff run already imported the batch into the isolated database, so
    the PostgreSQL store performs the final current/tombstone/scope check;
    otherwise the pipeline checks against an in-memory copy of the corpus.
    """
    as_of = None if dataset is None else dataset.as_of
    if database_url and dataset is not None and dataset.kind == "handoff":
        from backend.rag.infra.retrieval.postgres_retrieval import (  # noqa: PLC0415
            PostgresRetrievalStore,
        )

        store = PostgresRetrievalStore(create_rag_engine(database_url))

        def truth(_articles: "Sequence[ArticleDocument]") -> "RetrievalStore":
            return store

        return as_of, truth
    return as_of, None


def _systems(
    options: "SystemOptions",
    database_url: str | None,
    dataset: "SearchDataset | None" = None,
) -> "list[SearchSystem]":
    from backend.rag.evaluation.search_eval.factory import (  # noqa: PLC0415
        SearchEvalConfigError,
        build_systems,
    )

    try:
        from backend.rag.evaluation.search_eval.dataset import (  # noqa: PLC0415
            FIXTURE_AS_OF,
        )

        as_of, truth = _pipeline_inputs(database_url, dataset)
        return build_systems(
            options,
            _postgres_lexical(database_url, dataset),
            as_of=as_of or FIXTURE_AS_OF,
            truth=truth,
        )
    except SearchEvalConfigError as error:
        typer.echo(f"search_eval_invalid: {error}", err=True)
        raise typer.Exit(2) from None


DEFAULT_CONFIGS = "memory-bm25,memory-semantic,memory-hybrid"
Configs = Annotated[str, typer.Option(help="comma list, e.g. es-bm25,es-hybrid")]
EsUrl = Annotated[
    str | None, typer.Option(envvar="EVENTLENS_RAG_EVAL_ELASTICSEARCH_URL")
]
EvalDb = Annotated[str | None, typer.Option(envvar="EVENTLENS_RAG_EVAL_DATABASE_URL")]
EmbeddingChoice = Annotated[str, typer.Option(help="fake-bge-m3 | bge-m3")]
Revision = Annotated[str | None, typer.Option()]


@app.command("search-eval")
def search_eval(  # noqa: PLR0913, PLR0917 - benchmark knobs
    output: Annotated[Path, typer.Option()] = SEARCH_EVAL_OUTPUT,
    repeats: Annotated[int, typer.Option(min=3, max=20)] = 3,
    configs: Configs = DEFAULT_CONFIGS,
    elasticsearch_url: EsUrl = None,
    database_url: EvalDb = None,
    embeddings: EmbeddingChoice = "fake-bge-m3",
    bge_m3_revision: Annotated[
        str | None, typer.Option(envvar="EVENTLENS_RAG_BGE_M3_REVISION")
    ] = None,
    rerank_revision: Annotated[
        str | None, typer.Option(envvar="EVENTLENS_RAG_RERANK_REVISION")
    ] = None,
    rerank: Annotated[
        str, typer.Option(help="none | fake (mechanics) | cross-encoder")
    ] = "none",
    device: Annotated[str, typer.Option()] = "cpu",
    label: Annotated[str, typer.Option(help="report file prefix")] = "search-eval",
    articles: PathOption = None,
    manifest: PathOption = None,
    questions: PathOption = None,
    prescreen: Annotated[
        Path | None,
        typer.Option(dir_okay=False, exists=True, help="AI prescreen verdicts"),
    ] = None,
    expect_export_id: Annotated[
        str | None,
        typer.Option(help="refuse any other corpus, including the fixture"),
    ] = None,
    expect_articles: Annotated[int | None, typer.Option(min=1)] = None,
    chunker: Annotated[
        str, typer.Option(help="default index chunker (per config: name@chunker)")
    ] = "paragraph-900-1.v1",
    compare: Annotated[
        str | None, typer.Option(help="paired diffs: baseline:candidate,...")
    ] = None,
    checkpoint_dir: Annotated[
        Path | None, typer.Option(file_okay=False, help="per-config checkpoints")
    ] = None,
    resume: Annotated[  # noqa: FBT002
        bool, typer.Option(help="reuse checkpoints and an already-imported DB")
    ] = False,
    review_dir: Annotated[
        Path | None,
        typer.Option(file_okay=False, help="private review sheet (holds text)"),
    ] = None,
    fixture_dir: Annotated[
        Path | None,
        typer.Option(
            file_okay=False,
            exists=True,
            help="another synthetic fixture (e.g. freshness-updates-v1)",
        ),
    ] = None,
    trace_dir: Annotated[
        Path | None,
        typer.Option(
            file_okay=False,
            help="per-layer text-free traces (first repeat, pipeline configs)",
        ),
    ] = None,
) -> None:
    """Search-selection benchmark (>= 3 repeats per configuration).

    Without ``--articles/--manifest/--questions`` it scores the synthetic
    fixture. With them it validates the handoff batch, imports it atomically
    into the (empty, ``*_eval``) database when one is given, reads the
    revisions back from PostgreSQL and scores the external labels.
    ``--expect-export-id``/``--expect-articles`` make the run fail instead of
    silently falling back to the fixture or another batch.
    """
    from backend.rag.evaluation.metrics import canonical  # noqa: PLC0415
    from backend.rag.evaluation.search_eval.factory import (  # noqa: PLC0415
        SystemOptions,
    )
    from backend.rag.evaluation.search_eval.runner import (  # noqa: PLC0415
        evaluate,
        failing_configurations,
    )

    dataset, imported = _eval_dataset(
        (articles, manifest, questions, prescreen),
        expect_export_id,
        expect_articles,
        database_url,
        resume=resume,
        fixture_dir=fixture_dir,
    )
    options = SystemOptions(
        configs=tuple(c.strip() for c in configs.split(",") if c.strip()),
        elasticsearch_url=elasticsearch_url,
        embeddings=embeddings,
        bge_m3_revision=bge_m3_revision,
        rerank_revision=rerank_revision,
        rerank=rerank,
        device=device,
        chunker=chunker,
    )
    systems = _systems(options, database_url, dataset)
    metadata: dict[str, Json] = {
        **_git_metadata(),
        "embeddings": embeddings,
        "bge_m3_revision": bge_m3_revision,
        "rerank_revision": rerank_revision,
        "rerank": rerank,
        "device": device,
        "default_chunker": chunker,
        "python": sys.version.split()[0],
        "offline": True,
        "dataset_kind": dataset.kind,
        "import": imported,
    }
    sheet = None
    if review_dir is not None:
        from backend.rag.evaluation.search_eval.evidence_sheet import (  # noqa: PLC0415
            EvidenceSheet,
        )

        sheet = EvidenceSheet(dataset)
    traces = None if trace_dir is None else _TraceSink(trace_dir, label)
    sinks = [s.add for s in (sheet, traces) if s is not None]

    def sink(name: str, question: "EvalQuestion", answer: "SystemAnswer") -> None:
        for add in sinks:
            add(name, question, answer)

    deterministic, measured = evaluate(
        dataset,
        systems,
        repeats,
        metadata,
        on_progress=lambda line: typer.echo(f"search-eval: {line}", err=True),
        sink=sink if sinks else None,
        checkpoints=None
        if checkpoint_dir is None
        else _FileCheckpoints(checkpoint_dir, resume=resume),
    )
    if sheet is not None and review_dir is not None:
        written = sheet.write(review_dir)
        typer.echo(f"search-eval: private review sheet ({written} files) {review_dir}")
    output.mkdir(parents=True, exist_ok=True)
    _ = (output / f"{label}.json").write_text(
        canonical(deterministic) + "\n", encoding="utf-8"
    )
    _ = (output / f"{label}.measured.json").write_text(
        canonical(measured) + "\n", encoding="utf-8"
    )
    typer.echo(f"search-eval: report {output / f'{label}.json'}")
    typer.echo(f"search-eval: digest {deterministic['report_digest']}")
    if compare:
        _write_comparison(
            output / f"{label}.compare.json", deterministic, dataset, compare
        )
    violations = failing_configurations(deterministic)
    if violations:
        typer.echo(f"search-eval: scope violations or errors in {violations}", err=True)
        raise typer.Exit(SMOKE_FAILED_EXIT)


class _TraceSink:
    """Writes one JSON line per (configuration, question) with the layer trace.

    Text-free: IDs, offsets, ranks and counters only (no article or query
    text), so the files may sit next to the shared reports.
    """

    def __init__(self, directory: Path, label: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path: Path = directory / f"{label}.trace.jsonl"
        _ = self.path.write_text("", encoding="utf-8")

    def add(self, name: str, question: "EvalQuestion", answer: "SystemAnswer") -> None:
        if answer.evidence is None or answer.evidence.trace is None:
            return
        line = json.dumps(
            {
                "configuration": name,
                "question_id": question.question_id,
                "repeat": 1,  # the runner's sink only sees the first repeat
                "trace": answer.evidence.trace,
            },
            sort_keys=True,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            _ = handle.write(line + "\n")


class _FileCheckpoints:
    """One JSON file per finished configuration (text-free report halves)."""

    def __init__(self, directory: Path, *, resume: bool) -> None:
        self.directory: Path = directory
        self.resume: bool = resume
        directory.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-." else "_" for c in name)
        return self.directory / f"{safe}.json"

    def load(self, name: str, key: str) -> "tuple[Json, Json] | None":
        path = self._path(name)
        if not self.resume or not path.exists():
            return None
        saved = TypeAdapter(dict[str, object]).validate_json(path.read_bytes())
        if saved.get("key") != key:
            typer.echo(f"search-eval: checkpoint {path.name} is stale; rerun", err=True)
            return None
        return cast("Json", saved["entry"]), cast("Json", saved["measured"])

    def save(self, name: str, key: str, entry: "Json", measured: "Json") -> None:
        from backend.rag.evaluation.metrics import canonical  # noqa: PLC0415

        payload: Json = {"key": key, "entry": entry, "measured": measured}
        _ = self._path(name).write_text(canonical(payload) + "\n", encoding="utf-8")


def _eval_dataset(  # noqa: PLR0913 - inputs of one dataset decision
    paths: tuple[Path | None, Path | None, Path | None, Path | None],
    expect_export_id: str | None,
    expect_articles: int | None,
    database_url: str | None,
    *,
    resume: bool = False,
    fixture_dir: Path | None = None,
) -> "tuple[SearchDataset, Json]":
    """Fixture, or the external batch (imported + read back when a DB is given)."""
    from backend.rag.evaluation.search_eval.dataset import load_dataset  # noqa: PLC0415
    from backend.rag.evaluation.search_eval.handoff_dataset import (  # noqa: PLC0415
        HandoffDatasetError,
        load_handoff_dataset,
    )

    articles, manifest, questions, prescreen = paths
    given = [p is not None for p in (articles, manifest, questions)]
    if not any(given):
        if expect_export_id is not None or expect_articles is not None:
            typer.echo(
                (
                    "search_eval_invalid: an expected corpus was named but no "
                    "--articles/--manifest/--questions were given; the fixture is "
                    "never substituted"
                ),
                err=True,
            )
            raise typer.Exit(2)
        return (
            load_dataset() if fixture_dir is None else load_dataset(fixture_dir)
        ), None
    if not all(given):
        typer.echo(
            "search_eval_invalid: --articles, --manifest and --questions go together",
            err=True,
        )
        raise typer.Exit(2)
    assert articles is not None  # noqa: S101
    assert manifest is not None  # noqa: S101
    assert questions is not None  # noqa: S101
    articles_bytes, manifest_bytes = articles.read_bytes(), manifest.read_bytes()
    imported: Json = None
    stored = None
    if database_url:
        from backend.rag.infra.postgres.source_versions import (  # noqa: PLC0415
            PostgresSourceVersions,
        )

        _require_eval(database_url)
        populated = bool(_table_counts(database_url)["rag_article_documents"])
        if populated and not resume:
            typer.echo(
                "search_eval_invalid: database must be isolated and empty", err=True
            )
            raise typer.Exit(2)
        report = _validated(articles, manifest)
        if not report.ok:
            typer.echo("search_eval_invalid: handoff batch rejected", err=True)
            raise typer.Exit(2)
        # Resume never writes: the read-back below must equal the batch exactly.
        imported = cast(
            "Json",
            {"resumed": True, "tables": _table_counts(database_url)}
            if populated
            else _import(database_url, report),
        )
        stored = PostgresSourceVersions(rag_engine(database_url)).current_documents()
    try:
        loaded = load_handoff_dataset(
            articles_bytes,
            manifest_bytes,
            questions.read_bytes(),
            prescreen=None if prescreen is None else prescreen.read_bytes(),
            stored=stored,
            expected_export_id=expect_export_id,
        )
    except HandoffDatasetError as error:
        typer.echo(f"search_eval_invalid: {error}", err=True)
        raise typer.Exit(2) from None
    dataset = loaded.dataset
    if expect_articles is not None and len(dataset.articles) != expect_articles:
        typer.echo(
            (
                f"search_eval_invalid: {len(dataset.articles)} articles, "
                f"expected {expect_articles}"
            ),
            err=True,
        )
        raise typer.Exit(2)
    return dataset, imported


def _write_comparison(
    path: Path, report: "dict[str, Json]", dataset: "SearchDataset", pairs: str
) -> None:
    from backend.rag.evaluation.metrics import canonical  # noqa: PLC0415
    from backend.rag.evaluation.search_eval.compare import (  # noqa: PLC0415
        compare as compare_pairs,
    )

    wanted = [
        (left.strip(), right.strip())
        for left, _, right in (p.partition(":") for p in pairs.split(","))
        if left.strip() and right.strip()
    ]
    split_of = {q.question_id: q.split for q in dataset.questions}
    payload: dict[str, Json] = {
        "report_digest": report["report_digest"],
        "splits": {
            split: compare_pairs(report, wanted, split_of, split)
            for split in sorted(set(split_of.values()))
        },
    }
    _ = path.write_text(canonical(payload) + "\n", encoding="utf-8")
    typer.echo(f"search-eval: comparison {path}")


@app.command("search-review-sheet")
def search_review_sheet(  # noqa: PLR0913, PLR0917 - benchmark knobs
    output: Annotated[Path, typer.Option()] = REVIEW_OUTPUT,
    configs: Configs = DEFAULT_CONFIGS,
    elasticsearch_url: EsUrl = None,
    database_url: EvalDb = None,
    embeddings: EmbeddingChoice = "fake-bge-m3",
    bge_m3_revision: Annotated[
        str | None, typer.Option(envvar="EVENTLENS_RAG_BGE_M3_REVISION")
    ] = None,
    depth: Annotated[int, typer.Option(min=1, max=50)] = 10,
) -> None:
    """Pooled top-k candidates per question as CSV + HTML for human labels."""
    from backend.rag.evaluation.search_eval.dataset import load_dataset  # noqa: PLC0415
    from backend.rag.evaluation.search_eval.factory import (  # noqa: PLC0415
        SystemOptions,
    )
    from backend.rag.evaluation.search_eval.review import (  # noqa: PLC0415
        build_review_sheet,
    )

    systems = _systems(
        SystemOptions(
            configs=tuple(c.strip() for c in configs.split(",") if c.strip()),
            elasticsearch_url=elasticsearch_url,
            embeddings=embeddings,
            bge_m3_revision=bge_m3_revision,
        ),
        database_url,
    )
    sheet = build_review_sheet(load_dataset(), systems, depth)
    output.mkdir(parents=True, exist_ok=True)
    _ = (output / "search-review.csv").write_text(sheet.csv_text, encoding="utf-8")
    _ = (output / "search-review.html").write_text(sheet.html_text, encoding="utf-8")
    typer.echo(f"search-review-sheet: {sheet.rows} rows -> {output}")


@app.command("search-label-audit")
def search_label_audit(  # noqa: PLR0913, PLR0917 - explicit label/corpus inputs
    questions: Annotated[Path, typer.Option(dir_okay=False, exists=True)],
    source: Annotated[
        list[Path],
        typer.Option(dir_okay=False, exists=True, help="source label set(s)"),
    ],
    articles: Annotated[Path, typer.Option(dir_okay=False, exists=True)],
    manifest: Annotated[Path, typer.Option(dir_okay=False, exists=True)],
    output: Annotated[Path, typer.Option(dir_okay=False)],
    facets: PathOption = None,
) -> None:
    """Audit a derived candidate label set (IDs, hashes, offsets, filters, status).

    Text-free output; exit 2 when any issue is found.
    """
    from datetime import datetime  # noqa: PLC0415

    from backend.rag.domain.article_ingest import IngestClock  # noqa: PLC0415
    from backend.rag.domain.handoff import document_from_handoff  # noqa: PLC0415
    from backend.rag.evaluation.search_eval.label_audit import (  # noqa: PLC0415
        audit_label_set,
    )

    report = _validated(articles, manifest)
    if not report.ok:
        typer.echo("search_label_audit_invalid: handoff batch rejected", err=True)
        raise typer.Exit(HANDOFF_INVALID_EXIT)
    raw_manifest = cast("dict[str, object]", json.loads(manifest.read_bytes()))
    at = datetime.fromisoformat(str(raw_manifest["exported_at"]))
    clock = IngestClock(ingested_at=at, processed_at=at)
    revisions = {
        str(r.article_id): document_from_handoff(r, clock) for r in report.records
    }
    versions = {str(r.article_id): str(r.article_version_id) for r in report.records}
    derived = cast("dict[str, object]", json.loads(questions.read_bytes()))
    rows = [
        row
        for path in source
        for row in cast(
            "list[dict[str, object]]", json.loads(path.read_bytes())["questions"]
        )
    ]
    audit = audit_label_set(
        derived,
        rows,
        revisions,
        versions,
        facets=None
        if facets is None
        else cast("dict[str, object]", json.loads(facets.read_bytes())),
    )
    audit["inputs"] = {
        "questions_sha256": hashlib.sha256(questions.read_bytes()).hexdigest(),
        "articles_sha256": report.articles_sha256,
        "sources_sha256": [hashlib.sha256(p.read_bytes()).hexdigest() for p in source],
        "facets_sha256": None
        if facets is None
        else hashlib.sha256(facets.read_bytes()).hexdigest(),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    _ = output.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    typer.echo(f"search-label-audit: ok={audit['ok']} -> {output}")
    if not audit["ok"]:
        raise typer.Exit(HANDOFF_INVALID_EXIT)


@app.command("search-eval-diagnose")
def search_eval_diagnose(  # noqa: PLR0913, PLR0917 - explicit inputs
    traces: Annotated[Path, typer.Option(dir_okay=False, exists=True)],
    questions: Annotated[Path, typer.Option(dir_okay=False, exists=True)],
    facets: Annotated[Path, typer.Option(dir_okay=False, exists=True)],
    eval_manifest: Annotated[Path, typer.Option(dir_okay=False, exists=True)],
    articles: Annotated[Path, typer.Option(dir_okay=False, exists=True)],
    manifest: Annotated[Path, typer.Option(dir_okay=False, exists=True)],
    output: Annotated[Path, typer.Option(file_okay=False)],
) -> None:
    """Layer diagnostics of ``search-eval --trace-dir`` traces (text-free output).

    Writes ``diagnostics.json`` (aggregates per configuration and split,
    pre-registered paired comparisons with cluster bootstrap) and
    ``per-question-comparison.json`` (every question under every configuration).
    """
    from datetime import datetime  # noqa: PLC0415

    from backend.rag.domain.article_ingest import IngestClock  # noqa: PLC0415
    from backend.rag.domain.handoff import document_from_handoff  # noqa: PLC0415
    from backend.rag.evaluation.search_eval import diagnostics  # noqa: PLC0415

    report = _validated(articles, manifest)
    if not report.ok:
        typer.echo("search_eval_diagnose_invalid: handoff batch rejected", err=True)
        raise typer.Exit(HANDOFF_INVALID_EXIT)
    raw_manifest = cast("dict[str, object]", json.loads(manifest.read_bytes()))
    at = datetime.fromisoformat(str(raw_manifest["exported_at"]))
    clock = IngestClock(ingested_at=at, processed_at=at)
    corpus = {
        str(d.article_id): (d.source_domain, d.source_published_at)
        for d in (document_from_handoff(r, clock) for r in report.records)
    }
    plan = cast("dict[str, object]", json.loads(eval_manifest.read_bytes()))
    configurations = [
        str(c) for c in cast("list[object]", plan.get("configurations") or [])
    ]
    try:
        diagnostics.check_identities(
            plan,
            {
                "questions_v2_sha256": questions.read_bytes(),
                "facets_v2_sha256": facets.read_bytes(),
            },
        )
        labels = diagnostics.load_labels(
            cast("dict[str, object]", json.loads(questions.read_bytes())),
            cast("dict[str, object]", json.loads(facets.read_bytes())),
            plan,
        )
        traced = diagnostics.collect_traces(
            traces.read_text(encoding="utf-8").splitlines(), labels, configurations
        )
    except diagnostics.DiagnosticsInputError as error:
        typer.echo(f"search_eval_diagnose_invalid: {error}", err=True)
        raise typer.Exit(HANDOFF_INVALID_EXIT) from error
    rows: dict[str, dict[str, dict[str, Json]]] = {
        name: {
            qid: diagnostics.question_row(labels[qid], trace, corpus)
            for qid, trace in sorted(per_q.items())
        }
        for name, per_q in traced.items()
    }
    splits = sorted({label.split for label in labels.values()})
    summary: dict[str, Json] = {
        name: {
            "all": diagnostics.aggregate(list(per_q.values())),
            "by_split": {
                split: diagnostics.aggregate(
                    [r for q, r in per_q.items() if labels[q].split == split]
                )
                for split in splits
            },
            "questions_traced": len(per_q),
        }
        for name, per_q in rows.items()
    }
    pairs = [
        (str(p[0]), str(p[1]))
        for p in cast("list[list[object]]", plan.get("pre_registered_pairs") or [])
    ]
    digests = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in (
            ("traces", traces),
            ("questions", questions),
            ("facets", facets),
            ("eval_manifest", eval_manifest),
        )
    }
    output.mkdir(parents=True, exist_ok=True)
    result: dict[str, Json] = {
        "schema": "eventlens-search-diagnostics.v1",
        "inputs_sha256": cast("dict[str, Json]", digests),
        "configurations": summary,
        "pairs": diagnostics.compare_pairs(rows, pairs, labels),
        "note": "labels are AI-reviewed candidates; containment != semantic proof",
    }
    _ = (output / "diagnostics.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    by_question: dict[str, Json] = {
        qid: {name: per_q[qid] for name, per_q in rows.items() if qid in per_q}
        for qid in sorted(labels)
    }
    _ = (output / "per-question-comparison.json").write_text(
        json.dumps(
            {
                "inputs_sha256": cast("dict[str, Json]", digests),
                "questions": by_question,
            },
            ensure_ascii=False,
            indent=1,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    typer.echo(f"search-eval-diagnose: {len(rows)} configurations -> {output}")


OLLAMA_OUTPUT = ROOT / ".private/ollama-bench"


def _private_output(path: Path) -> None:
    # Private reports hold model output; the checkpoint holds prompts too.
    if ".private" not in path.absolute().parts:
        typer.echo("ollama_bench_invalid: --output must be inside .private/", err=True)
        raise typer.Exit(2)


def _bench_dataset(
    paths: tuple[Path | None, Path | None, Path | None],
    expect_export_id: str | None,
    question_ids: Path | None,
) -> "SearchDataset":
    from dataclasses import replace  # noqa: PLC0415

    from backend.rag.evaluation.search_eval.dataset import load_dataset  # noqa: PLC0415

    articles, manifest, questions = paths
    dataset = (
        load_dataset()
        if articles is None and manifest is None and questions is None
        else _eval_dataset(
            (articles, manifest, questions, None), expect_export_id, None, None
        )[0]
    )
    if question_ids is None:
        return dataset
    wanted = TypeAdapter(list[str]).validate_json(question_ids.read_bytes())
    known = {q.question_id: q for q in dataset.questions}
    missing = [q for q in wanted if q not in known]
    if missing:
        typer.echo(f"ollama_bench_invalid: unknown questions {missing}", err=True)
        raise typer.Exit(2)
    return replace(dataset, questions=tuple(known[q] for q in wanted))


def _write_bench_reports(
    report: "dict[str, Json]", output: Path, label: str, shared: Path | None
) -> None:
    from backend.rag.evaluation.metrics import canonical  # noqa: PLC0415
    from backend.rag.evaluation.ollama_bench import shared_view  # noqa: PLC0415

    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{label}.json"
    _ = path.write_text(
        canonical({**report, **_git_metadata()}) + "\n", encoding="utf-8"
    )
    typer.echo(f"ollama-bench: report {path} ({report['status']})")
    if shared is not None:
        shared.parent.mkdir(parents=True, exist_ok=True)
        _ = shared.write_text(
            canonical({**shared_view(report), **_git_metadata()}) + "\n",
            encoding="utf-8",
        )
        typer.echo(f"ollama-bench: shared report {shared}")
    typer.echo(f"ollama-bench: digest {report['report_digest']}")


@dataclass(slots=True)
class _BenchEvidence:
    """Product payload per question, searched once and cached."""

    system: "SearchSystem"
    articles: "Mapping[UUID, ArticleDocument]"
    answers: "dict[str, SystemAnswer]" = field(default_factory=dict)

    def answer(self, question: "EvalQuestion") -> "SystemAnswer":
        if question.question_id not in self.answers:
            self.answers[question.question_id] = self.system.search(question)
        return self.answers[question.question_id]

    def passages(self, question: "EvalQuestion") -> "Sequence[RankedPassage]":
        from backend.rag.evaluation.search_eval.pipeline import (  # noqa: PLC0415
            payload_passages,
        )

        return payload_passages(self.answer(question), self.articles)

    def info(self, question: "EvalQuestion") -> "dict[str, Json]":
        stages = self.answer(question).evidence
        if stages is None:
            return {"source": "ranked"}
        return {
            "source": "payload",
            "app_payload_tokens": stages.payload_tokens,
            "app_token_budget": stages.token_budget,
            "truncated_spans": stages.truncated_spans,
            "omitted_articles": stages.omitted_articles,
            "fill_spans_sent": stages.fill_spans_sent,
        }


def _write_plan(
    dataset: "SearchDataset",
    evidence: "Callable[[EvalQuestion], Sequence[RankedPassage]]",
    info: "Callable[[EvalQuestion], dict[str, Json]]",
    output: Path,
    label: str,
) -> None:
    """Text-free prompt sizes per question (no model call)."""
    from backend.rag.evaluation.metrics import canonical  # noqa: PLC0415
    from backend.rag.evaluation.ollama_bench import (  # noqa: PLC0415
        SYSTEM_PROMPT,
        user_prompt,
    )

    rows: dict[str, Json] = {}
    for question in dataset.questions:
        passages = tuple(evidence(question))
        rows[question.question_id] = {
            **info(question),
            "payload_spans": len(passages),
            "system_chars": len(SYSTEM_PROMPT),
            "user_chars": len(user_prompt(question, passages)),
        }
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{label}.plan.json"
    _ = path.write_text(canonical({"questions": rows}) + "\n", encoding="utf-8")
    typer.echo(f"ollama-bench: plan {path} (no generation)")


@app.command("ollama-bench")
def ollama_bench(  # noqa: PLR0913, PLR0917 - benchmark knobs
    output: Annotated[Path, typer.Option()] = OLLAMA_OUTPUT,
    base_url: Annotated[
        str | None, typer.Option(envvar="EVENTLENS_EVAL_OLLAMA_BASE_URL")
    ] = None,
    models: Annotated[
        str | None, typer.Option(envvar="EVENTLENS_EVAL_OLLAMA_MODEL")
    ] = None,
    timeout: Annotated[
        float, typer.Option(help="offline benchmark timeout (s)")
    ] = 600.0,
    num_thread: Annotated[
        int | None, typer.Option(envvar="EVENTLENS_EVAL_OLLAMA_NUM_THREAD")
    ] = None,
    label: Annotated[str, typer.Option(help="report file prefix")] = "ollama-bench",
    checkpoint: Annotated[
        Path | None,
        typer.Option(help="response store (default <output>/<label>.checkpoint)"),
    ] = None,
    shared_output: Annotated[
        Path | None, typer.Option(help="also write the text-free report here")
    ] = None,
    articles: PathOption = None,
    manifest: PathOption = None,
    questions: PathOption = None,
    expect_export_id: Annotated[str | None, typer.Option()] = None,
    question_ids: Annotated[
        Path | None,
        typer.Option(dir_okay=False, exists=True, help="JSON list, fixed beforehand"),
    ] = None,
    evidence_config: Annotated[
        str, typer.Option(help="retrieval configuration that supplies evidence")
    ] = "memory-bm25",
    elasticsearch_url: EsUrl = None,
    evidence_k: Annotated[
        int, typer.Option(min=1, max=60, help="max passages sent (payload configs)")
    ] = 5,
    allow_evidence_truncation: Annotated[  # noqa: FBT002
        bool, typer.Option(help="send only the first evidence_k payload spans")
    ] = False,
    plan_only: Annotated[  # noqa: FBT002
        bool,
        typer.Option(help="retrieve and size the prompts only; never generate"),
    ] = False,
    embeddings: EmbeddingChoice = "fake-bge-m3",
    bge_m3_revision: Annotated[
        str | None, typer.Option(envvar="EVENTLENS_RAG_BGE_M3_REVISION")
    ] = None,
    device: Annotated[str, typer.Option()] = "cpu",
) -> None:
    """Offline Qwen/Ollama synthesis benchmark (private network; never production).

    Evidence comes from ``--evidence-config`` over the fixture or, with
    ``--articles/--manifest/--questions``, over the external batch. The model
    never sees labels; production provider activation is not touched. The
    model digest is pinned before generation; every response is stored in the
    checkpoint as it arrives and a rerun resumes from it. Exit 0 only when the
    run is complete; a partial or failed report is still written (exit 1).
    """
    from backend.rag.evaluation.ollama_bench import (  # noqa: PLC0415
        OllamaBenchConfigError,
        OllamaBenchPreflightError,
        OllamaClient,
        preflight,
        run_bench,
    )
    from backend.rag.evaluation.ollama_checkpoint import (  # noqa: PLC0415
        CheckpointLocationError,
        CheckpointStore,
    )
    from backend.rag.evaluation.search_eval.factory import (  # noqa: PLC0415
        SystemOptions,
        build_systems,
    )

    if not base_url or not models:
        typer.echo("ollama_bench_invalid: base URL and models are required", err=True)
        raise typer.Exit(2)
    _private_output(output)
    try:
        client = OllamaClient(base_url, timeout_seconds=timeout, num_thread=num_thread)
        store = CheckpointStore(checkpoint or output / f"{label}.checkpoint")
    except (OllamaBenchConfigError, CheckpointLocationError) as error:
        typer.echo(f"ollama_bench_invalid: {error}", err=True)
        raise typer.Exit(2) from None
    names = [m.strip() for m in models.split(",") if m.strip()]
    try:
        pinned = preflight(client, names)  # before indexing: fail fast
    except OllamaBenchPreflightError as error:
        typer.echo(f"ollama_bench_preflight_failed: {error}", err=True)
        raise typer.Exit(2) from None
    typer.echo(f"ollama-bench: pinned {pinned}", err=True)
    dataset = _bench_dataset(
        (articles, manifest, questions), expect_export_id, question_ids
    )
    (system,) = build_systems(
        SystemOptions(
            configs=(evidence_config,),
            elasticsearch_url=elasticsearch_url,
            embeddings=embeddings,
            bge_m3_revision=bge_m3_revision,
            device=device,
        ),
        None,
        as_of=dataset.as_of,
    )
    _ = system.prepare(dataset.articles)
    source = _BenchEvidence(system, {a.article_id: a for a in dataset.articles})
    if plan_only:
        try:
            _write_plan(dataset, source.passages, source.info, output, label)
        finally:
            system.close()
        return
    try:
        report = run_bench(
            dataset,
            source.passages,
            client,
            names,
            on_progress=lambda line: typer.echo(f"ollama-bench: {line}", err=True),
            store=store,
            evidence_k=evidence_k,
            evidence_info=source.info,
            evidence_configuration=system.describe(),
            allow_evidence_truncation=allow_evidence_truncation,
        )
    except OllamaBenchPreflightError as error:
        typer.echo(f"ollama_bench_preflight_failed: {error}", err=True)
        raise typer.Exit(2) from None
    finally:
        system.close()
    _write_bench_reports(report, output, label, shared_output)
    if report["status"] != "complete":
        raise typer.Exit(1)


@app.command("ollama-bench-report")
def ollama_bench_report(  # noqa: PLR0913, PLR0917 - dataset inputs
    checkpoint: Annotated[Path, typer.Option(file_okay=False, exists=True)],
    output: Annotated[Path, typer.Option()] = OLLAMA_OUTPUT,
    label: Annotated[str, typer.Option(help="report file prefix")] = "ollama-bench",
    shared_output: Annotated[Path | None, typer.Option()] = None,
    articles: PathOption = None,
    manifest: PathOption = None,
    questions: PathOption = None,
    expect_export_id: Annotated[str | None, typer.Option()] = None,
    question_ids: Annotated[
        Path | None, typer.Option(dir_okay=False, exists=True)
    ] = None,
) -> None:
    """Rebuild the bench report from a checkpoint without calling any model."""
    from backend.rag.evaluation.guards import no_network  # noqa: PLC0415
    from backend.rag.evaluation.ollama_bench import (  # noqa: PLC0415
        OllamaBenchPreflightError,
        rebuild_report,
    )
    from backend.rag.evaluation.ollama_checkpoint import (  # noqa: PLC0415
        CheckpointLocationError,
        CheckpointStore,
    )

    _private_output(output)
    dataset = _bench_dataset(
        (articles, manifest, questions), expect_export_id, question_ids
    )
    try:
        with no_network():
            report = rebuild_report(CheckpointStore(checkpoint), dataset)
    except (OllamaBenchPreflightError, CheckpointLocationError) as error:
        typer.echo(f"ollama_bench_invalid: {error}", err=True)
        raise typer.Exit(2) from None
    _write_bench_reports(report, output, label, shared_output)
    if report["status"] != "complete":
        raise typer.Exit(1)
