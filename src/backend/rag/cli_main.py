"""``eventlens-rag`` entry point: local RAG database maintenance and smokes.

Outer-ring entry point (like ``api_main``/``worker_main``): it drives
PostgreSQL adapters and diagnostics harnesses directly, so it lives beside the
composition root rather than in ``presentation``.
"""

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

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
    scalars,
    transaction,
)
from backend.rag.infra.postgres.migrations import LegacySchemaError
from backend.rag.infra.postgres.staging import PostgresStaging
from backend.rag.infra.postgres.store import PostgresRagStore

if TYPE_CHECKING:
    from backend.rag.application.ports.reranker import RerankerPort
    from backend.rag.application.ports.search_index import (
        ChunkLexicalIndex,
        TextEmbedder,
    )

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


# --- Search selection (handoff corpus + BM25/semantic/hybrid/rerank bench) ---

ArticlesFile = Annotated[Path, typer.Option(exists=True, dir_okay=False)]
ManifestFile = Annotated[Path, typer.Option(exists=True, dir_okay=False)]
EvalDatabaseUrl = Annotated[str, typer.Option(envvar="EVENTLENS_RAG_EVAL_DATABASE_URL")]
HANDOFF_REJECTED_EXIT = 1


def _handoff_report(articles: Path, manifest: Path) -> object:
    from backend.rag.application.handoff_validation import (  # noqa: PLC0415
        validate_handoff,
    )

    return validate_handoff(articles.read_bytes(), manifest.read_bytes())


@app.command("handoff-validate")
def handoff_validate(
    articles: ArticlesFile,
    manifest: ManifestFile,
    output: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Validate a handoff batch; the JSON report carries no article text."""
    from backend.rag.application.handoff_validation import (  # noqa: PLC0415
        HandoffReport,
    )

    report = TypeAdapter(HandoffReport).validate_python(
        _handoff_report(articles, manifest)
    )
    text = json.dumps(report.to_json(), ensure_ascii=False, indent=2, default=str)
    if output is not None:
        _ = output.write_text(text + "\n", encoding="utf-8")
    typer.echo(text if output is None else f"handoff report: {output}")
    summary = (
        f"handoff: ok={report.ok} errors={len(report.errors)}"
        f" warnings={len(report.warnings)} records={report.valid_records}"
    )
    typer.echo(summary)
    if not report.ok:
        raise typer.Exit(HANDOFF_REJECTED_EXIT)


@app.command("handoff-import")
def handoff_import(
    database_url: EvalDatabaseUrl,
    articles: ArticlesFile,
    manifest: ManifestFile,
) -> None:
    """Import a validated batch into an isolated *_eval database (idempotent)."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from backend.rag.application.handoff_validation import (  # noqa: PLC0415
        HandoffReport,
    )
    from backend.rag.application.use_cases.import_handoff import (  # noqa: PLC0415
        HandoffRejectedError,
        ImportHandoff,
    )
    from backend.rag.infra.access.fixture_access import (  # noqa: PLC0415
        FixtureAccessPolicy,
    )
    from backend.rag.infra.diagnostics.retrieval_smoke import CALLER  # noqa: PLC0415
    from backend.rag.infra.postgres.engine import rag_engine  # noqa: PLC0415
    from backend.rag.infra.postgres.eval_vectors import (  # noqa: PLC0415
        EvalDatabaseRefusedError,
        require_eval_database,
    )
    from backend.rag.infra.postgres.source_versions import (  # noqa: PLC0415
        PostgresSourceVersions,
    )

    report = TypeAdapter(HandoffReport).validate_python(
        _handoff_report(articles, manifest)
    )
    try:
        engine = rag_engine(database_url)
        _ = require_eval_database(engine)
        _ = apply_migrations(database_url)
        receipt = ImportHandoff(
            store=PostgresSourceVersions(engine),
            access=FixtureAccessPolicy(),
            clock=lambda: datetime.now(UTC),
        ).execute(report, CALLER)
    except StorageUnavailableError as error:
        raise _unavailable(error) from error
    except StorageOperationError as error:
        raise _storage_failed(error) from error
    except MIGRATION_ERRORS as error:
        raise _migration_failed(error) from error
    except (HandoffRejectedError, EvalDatabaseRefusedError) as error:
        typer.echo(f"{error.code}: {error}", err=True)
        raise typer.Exit(HANDOFF_REJECTED_EXIT) from error
    typer.echo(json.dumps(asdict(receipt)))


@app.command("search-questions")
def search_questions(  # noqa: PLR0913, PLR0917 - typer options
    articles: ArticlesFile,
    manifest: ManifestFile,
    candidates: Annotated[list[Path], typer.Option(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option()],
    seed: Annotated[str, typer.Option()] = "search-selection-v1",
    dev_fraction: Annotated[float, typer.Option(min=0.1, max=0.9)] = 0.4,
) -> None:
    """Resolve drafted candidates to offsets; write ``search-questions.v1``."""
    from backend.rag.application.handoff_validation import (  # noqa: PLC0415
        HandoffReport,
    )
    from backend.rag.domain.chunking import DEFAULT_POLICY  # noqa: PLC0415
    from backend.rag.evaluation.search_bench.candidates import (  # noqa: PLC0415
        build_question_set,
        load_candidates,
    )

    report = TypeAdapter(HandoffReport).validate_python(
        _handoff_report(articles, manifest)
    )
    if not report.ok or report.export_id is None:
        typer.echo("handoff_rejected: validate the batch first", err=True)
        raise typer.Exit(HANDOFF_REJECTED_EXIT)
    question_set = build_question_set(
        load_candidates([path.read_text(encoding="utf-8") for path in candidates]),
        report.records,
        export_id=report.export_id,
        chunker_version=DEFAULT_POLICY.version,
        seed=seed,
        dev_fraction=dev_fraction,
    )
    _ = output.write_text(question_set.model_dump_json(indent=2) + "\n", "utf-8")
    typer.echo(f"questions: {len(question_set.questions)} -> {output}")


MODEL_CHOICES = ("bge-m3-1024", "qwen3-0.6b-1024", "qwen3-0.6b-512")
ANALYZER_CHOICES = ("vi-standard.v1", "vi-standard-fold.v1", "vi-syllable-bigram.v1")


def _git(*args: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed git arguments
        ["git", *args],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip()


def _environment(
    device: str, database_versions: dict[str, str], extra: dict[str, object]
) -> dict[str, object]:
    import os  # noqa: PLC0415
    import platform  # noqa: PLC0415

    return {
        "commit": _git("rev-parse", "HEAD"),
        "worktree_dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "device": device,
        **database_versions,
        **extra,
    }


@app.command("search-bench")
def search_bench(  # noqa: PLR0913, PLR0915, PLR0917 - one wiring point
    database_url: EvalDatabaseUrl,
    questions: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    output_dir: Annotated[Path, typer.Option()],
    es_url: Annotated[str, typer.Option()] = "http://127.0.0.1:9200",
    analyzers: Annotated[list[str] | None, typer.Option()] = None,
    models: Annotated[list[str] | None, typer.Option()] = None,
    device: Annotated[str, typer.Option()] = "cpu",
    repeats: Annotated[int, typer.Option(min=1, max=10)] = 3,
    rerank: Annotated[bool, typer.Option()] = True,  # noqa: FBT002
    fake: Annotated[bool, typer.Option()] = False,  # noqa: FBT002
    query_clock: Annotated[str, typer.Option()] = "2026-10-26T14:26:14Z",
) -> None:
    """Index the imported snapshot and run A-E on an isolated *_eval DB.

    ``--fake`` swaps in the in-memory BM25 and hashing embedder (no
    Elasticsearch, no models) to check plumbing; its numbers mean nothing.
    """
    import resource  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415

    from backend.rag.application.use_cases.retrieve import (  # noqa: PLC0415
        bundled_retrieval_policy,
    )
    from backend.rag.domain.chunking import DEFAULT_POLICY  # noqa: PLC0415
    from backend.rag.evaluation.search_bench.bench import (  # noqa: PLC0415
        BenchInputs,
        Engines,
        run_search_selection,
    )
    from backend.rag.evaluation.search_bench.labels import (  # noqa: PLC0415
        QuestionSet,
    )
    from backend.rag.evaluation.search_bench.render import render  # noqa: PLC0415
    from backend.rag.evaluation.search_bench.runner import (  # noqa: PLC0415
        ArticleFacts,
    )
    from backend.rag.infra.access.fixture_access import (  # noqa: PLC0415
        FixtureAccessPolicy,
    )
    from backend.rag.infra.diagnostics.retrieval_smoke import CALLER  # noqa: PLC0415
    from backend.rag.infra.embeddings.hashing_embedder import (  # noqa: PLC0415
        HashingEmbedder,
    )
    from backend.rag.infra.embeddings.hf_cache import (  # noqa: PLC0415
        snapshot_revision,
    )
    from backend.rag.infra.embeddings.sentence_transformers_embedder import (  # noqa: PLC0415
        BGE_M3,
        QWEN3_EMBEDDING_06B,
        SentenceTransformerEmbedder,
    )
    from backend.rag.infra.postgres.engine import rag_engine  # noqa: PLC0415
    from backend.rag.infra.postgres.eval_vectors import (  # noqa: PLC0415
        PostgresEvalVectors,
        require_eval_database,
    )
    from backend.rag.infra.postgres.source_versions import (  # noqa: PLC0415
        PostgresSourceVersions,
    )
    from backend.rag.infra.rerankers.cross_encoder import (  # noqa: PLC0415
        BGE_RERANKER_V2_M3,
        CrossEncoderReranker,
    )
    from backend.rag.infra.retrieval.postgres_retrieval import (  # noqa: PLC0415
        PostgresRetrievalStore,
    )
    from backend.rag.infra.search.elasticsearch_bm25 import (  # noqa: PLC0415
        ElasticsearchBm25Index,
    )
    from backend.rag.infra.search.memory_bm25 import MemoryBm25Index  # noqa: PLC0415

    question_set = QuestionSet.model_validate_json(questions.read_bytes())
    engine = rag_engine(database_url)
    _ = require_eval_database(engine)
    corpus = PostgresSourceVersions(engine)
    documents = corpus.current_documents()
    provenance = {
        item.article_id: item.parse_version
        for item in corpus.provenance_for(tuple(d.article_id for d in documents))
    }
    facts = {
        d.article_id: ArticleFacts(
            source_domain=d.source_domain,
            parse_version=provenance.get(d.article_id, "unknown"),
            length=len(d.normalized_content),
        )
        for d in documents
    }
    with transaction(engine, read_only=True) as connection:
        versions = {
            "postgres": str(scalars(execute(connection, "SHOW server_version"))[0]),
            "pgvector": str(
                scalars(
                    execute(
                        connection,
                        "SELECT extversion FROM pg_extension WHERE extname='vector'",
                    )
                )[0]
            ),
        }
    lexical: dict[str, ChunkLexicalIndex] = {}
    embedders: dict[str, TextEmbedder] = {}
    reranker: RerankerPort | None = None
    extra: dict[str, object] = {"fake": fake}
    if fake:
        lexical["memory-bm25"] = MemoryBm25Index()
        embedders["fake-hashing-64"] = HashingEmbedder(64)
    else:
        from dataclasses import replace  # noqa: PLC0415
        from importlib import import_module  # noqa: PLC0415

        for analyzer in analyzers or list(ANALYZER_CHOICES):
            lexical[analyzer] = ElasticsearchBm25Index(
                es_url, analyzer, DEFAULT_POLICY.version
            )
        specs = {
            "bge-m3-1024": BGE_M3,
            "qwen3-0.6b-1024": QWEN3_EMBEDDING_06B,
            "qwen3-0.6b-512": replace(QWEN3_EMBEDDING_06B, dimension=512),
        }
        for name in models or list(MODEL_CHOICES):
            spec = specs[name]
            embedders[name] = SentenceTransformerEmbedder(
                replace(spec, revision=snapshot_revision(spec.model)), device=device
            )
        if rerank:
            reranker = CrossEncoderReranker(
                revision=snapshot_revision(BGE_RERANKER_V2_M3), device=device
            )
        libraries = {
            name: str(getattr(import_module(name), "__version__", "unknown"))
            for name in ("torch", "sentence_transformers", "transformers")
        }
        extra.update(libraries=libraries)
    real_models = {
        name: e
        for name, e in embedders.items()
        if isinstance(e, SentenceTransformerEmbedder)
    }
    engines = Engines(
        store=PostgresRetrievalStore(engine),
        lexical=lexical,
        embedders=embedders,
        vectors=PostgresEvalVectors(engine),
        reranker=reranker,
        release=lambda name: real_models[name].close() if name in real_models else None,
    )
    started = datetime.now().astimezone()
    report = run_search_selection(
        engines,
        BenchInputs(
            question_set=question_set,
            documents=documents,
            facts=facts,
            policy=bundled_retrieval_policy(),
            chunking=DEFAULT_POLICY,
            now=datetime.fromisoformat(query_clock),
            repeats=repeats,
            environment=_environment(device, versions, extra),
        ),
        FixtureAccessPolicy(),
        CALLER,
    )
    report["resources"] = {
        "started_at": started.isoformat(),
        "wall_seconds": (datetime.now().astimezone() - started).total_seconds(),
        # ru_maxrss is bytes on macOS and KiB on Linux.
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        / (1_048_576 if sys.platform == "darwin" else 1024),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _ = (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    _ = (output_dir / "summary.md").write_text(render(report) + "\n", "utf-8")
    typer.echo(f"search-bench: {output_dir / 'summary.md'}")


@app.command("search-review-sheet")
def search_review_sheet(
    articles: ArticlesFile,
    manifest: ManifestFile,
    questions: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option()],
    report: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
) -> None:
    """Write the PRIVATE review sheet (contains article text; never commit)."""
    from backend.rag.application.handoff_validation import (  # noqa: PLC0415
        HandoffReport,
    )
    from backend.rag.evaluation.search_bench.candidates import (  # noqa: PLC0415
        revisions,
    )
    from backend.rag.evaluation.search_bench.labels import (  # noqa: PLC0415
        QuestionSet,
    )
    from backend.rag.evaluation.search_bench.review import (  # noqa: PLC0415
        ArticleView,
        build_review_sheet,
        pool_from_report,
    )

    batch = TypeAdapter(HandoffReport).validate_python(
        _handoff_report(articles, manifest)
    )
    question_set = QuestionSet.model_validate_json(questions.read_bytes())
    corpus = revisions(batch.records)
    views = {
        r.article_id: ArticleView(
            title=r.title,
            source_domain=r.source_domain,
            published_at=None if r.published_at is None else r.published_at.isoformat(),
            revision_hash=corpus[r.article_id].content_hash,
            url=str(r.canonical_url),
        )
        for r in batch.records
    }
    pools = (
        {}
        if report is None
        else pool_from_report(
            TypeAdapter(dict[str, object]).validate_json(report.read_bytes())
        )
    )
    _ = output.write_text(
        build_review_sheet(question_set, corpus, views, pools), "utf-8"
    )
    typer.echo(f"review sheet (private, contains article text): {output}")


@app.command("search-apply-review")
def search_apply_review(  # noqa: PLR0913, PLR0917 - typer options
    articles: ArticlesFile,
    manifest: ManifestFile,
    questions: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    decisions: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    reviewer: Annotated[str, typer.Option()],
    output: Annotated[Path, typer.Option()],
) -> None:
    """Merge exported review decisions into a question set (offsets re-resolved)."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from backend.rag.application.handoff_validation import (  # noqa: PLC0415
        HandoffReport,
    )
    from backend.rag.evaluation.search_bench.candidates import (  # noqa: PLC0415
        revisions,
    )
    from backend.rag.evaluation.search_bench.labels import (  # noqa: PLC0415
        QuestionSet,
    )
    from backend.rag.evaluation.search_bench.review import (  # noqa: PLC0415
        ReviewDecisions,
        apply_review,
    )

    batch = TypeAdapter(HandoffReport).validate_python(
        _handoff_report(articles, manifest)
    )
    merged = apply_review(
        QuestionSet.model_validate_json(questions.read_bytes()),
        ReviewDecisions.model_validate_json(decisions.read_bytes()),
        revisions(batch.records),
        reviewer=reviewer,
        reviewed_at=datetime.now(UTC),
    )
    _ = output.write_text(merged.model_dump_json(indent=2) + "\n", "utf-8")
    statuses: dict[str, int] = {}
    for item in merged.questions:
        statuses[item.label_status] = statuses.get(item.label_status, 0) + 1
    typer.echo(f"reviewed question set: {output} {statuses}")
