"""``eventlens-rag`` entry point: local RAG database maintenance and smokes.

Outer-ring entry point (like ``api_main``/``worker_main``): it drives
PostgreSQL adapters and diagnostics harnesses directly, so it lives beside the
composition root rather than in ``presentation``.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from psycopg.rows import scalar_row
from pydantic import TypeAdapter

from backend.rag.application.use_cases.index import EnsureGeneration
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings
from backend.rag.infra.postgres.connection import StorageUnavailableError, connect
from backend.rag.infra.postgres.migrations import (
    LegacyDocumentError,
    LegacySchemaError,
    MigrationResourcesError,
)
from backend.rag.infra.postgres.migrations import (
    migrate as apply_migrations,
)
from backend.rag.infra.postgres.staging import PostgresStaging
from backend.rag.infra.postgres.store import PostgresRagStore

app = typer.Typer(no_args_is_help=True)
ROOT = Path(__file__).parents[3]
FIXTURE_ARTICLES = ROOT / "fixtures/rag/articles"
UNAVAILABLE_EXIT = 3
SMOKE_FAILED_EXIT = 2
EVAL_OUTPUT = ROOT / ".omo/evidence/T9"

DatabaseUrl = Annotated[str, typer.Option(envvar="EVENTLENS_RAG_DATABASE_URL")]


def _unavailable(error: StorageUnavailableError) -> typer.Exit:
    """Report typed storage failure without leaking the connection string."""
    typer.echo(f"{error.code}: {error}", err=True)
    return typer.Exit(UNAVAILABLE_EXIT)


@app.command()
def migrate(database_url: DatabaseUrl) -> None:
    """Apply RAG PostgreSQL and pgvector migrations."""
    try:
        apply_migrations(database_url)
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    except (
        LegacyDocumentError,
        LegacySchemaError,
        MigrationResourcesError,
    ) as error:
        typer.echo(f"rag_migration_failed: {error}", err=True)
        raise typer.Exit(1) from error
    typer.echo("rag migrations applied")


@app.command("load-fixtures")
def load_fixtures(
    database_url: DatabaseUrl,
    directory: Annotated[Path, typer.Option()] = FIXTURE_ARTICLES,
) -> None:
    """Persist fixture ArticleDocuments; lexical search is available immediately."""
    store = PostgresRagStore(database_url)
    if not directory.is_dir():
        typer.echo(f"rag_fixture_missing: {directory}", err=True)
        raise typer.Exit(1)
    paths = sorted(directory.glob("*.json"))
    try:
        for path in paths:
            article = ArticleDocument.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            store.persist_article(article)
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    typer.echo(f"rag persisted {len(paths)} fixture articles")


@app.command()
def rebuild(database_url: DatabaseUrl) -> None:
    """Stage every current article with fake embeddings, then flip atomically."""
    try:
        apply_migrations(database_url)
        with connect(database_url) as connection:
            rows = TypeAdapter(list[str]).validate_python(
                connection.cursor(row_factory=scalar_row)
                .execute("SELECT document_json::text FROM rag_current_lexical")
                .fetchall()
            )
        staging = PostgresStaging(database_url)
        ensure = EnsureGeneration(staging, FakeEmbeddings(), EmbeddingIdentity())
        staged = [
            ensure.execute(ArticleDocument.model_validate_json(row)) for row in rows
        ]
        published = staging.activate_all([item.generation for item in staged])
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    except (
        LegacyDocumentError,
        LegacySchemaError,
        MigrationResourcesError,
    ) as error:
        typer.echo(f"rag_migration_failed: {error}", err=True)
        raise typer.Exit(1) from error
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
        apply_migrations(database_url)
        load_corpus(database_url, articles, seed=5)
        results = measure(database_url, output, queries=queries, k=k, seed=5)
        version = pgvector_version(database_url)
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
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
