"""Adapter parity probe: same fixture flow, canonical outputs, one empty DB.

Run once per implementation (PYTHONPATH selects the source tree), each on its
own empty database, then diff the JSON. Volatile values (uuid4 job/chunk IDs,
timestamps, random tokens, latencies) are excluded.
"""

import importlib.util
import json
import sys
from pathlib import Path

import psycopg

from backend.rag.composition import FIXTURE_CALLER, build_runtime
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import QueryControl
from backend.rag.infra.diagnostics import retrieval_smoke as smoke
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.repositories.postgres_source_references import (
    PostgresSourceReferences,
)
from backend.rag.infra.retrieval import postgres_retrieval as pr
from backend.rag.infra.retrieval.identity_reranker import IdentityReranker
from backend.shared.contracts.article import Article

url, out = sys.argv[1], Path(sys.argv[2])
ROOT = Path(smoke.__file__).parents[5]
FIXTURES = ROOT / "fixtures/rag"
NEW = importlib.util.find_spec("backend.rag.infra.postgres.engine") is not None
if NEW:
    from backend.rag.infra.postgres.engine import rag_engine

    def adapter(cls, *args):  # noqa: ANN001, ANN002, ANN202
        return cls(rag_engine(url), *args)
else:

    def adapter(cls, *args):  # noqa: ANN001, ANN002, ANN202
        return cls(url, *args)


def summarize(result):  # noqa: ANN001, ANN201
    data = smoke.summarize(result)
    data["metrics"]["stages"] = [[s[0], s[2], s[3]] for s in data["metrics"]["stages"]]
    return data


def ask(service, text):  # noqa: ANN001, ANN201
    query = BriefingQuery.model_validate(
        {
            "schema_version": "briefing_query.v1",
            "query": text,
            "publication_time_basis": "source_published_at",
            "result_limit": 20,
            "context_budget": 12_000,
        }
    )
    return summarize(service.execute(query, smoke.CALLER, QueryControl(30.0)))


report: dict[str, object] = {"implementation": "sqlalchemy" if NEW else "psycopg"}
documents = smoke.load_fixture_corpus(url, FIXTURES)
service = smoke._service(url, IdentityReranker())  # noqa: SLF001
texts = [q.text for q in smoke.questions(FIXTURES)]
report["lexical"] = [ask(service, t) for t in texts]
smoke.enrich_fixture_corpus(url, FIXTURES, documents)
report["hybrid"] = [ask(service, t) for t in texts]

store = adapter(PostgresRagStore)
corrected = documents[0].model_copy(
    update={
        "normalized_content": documents[0].normalized_content + " Correction.",
        "raw_content": documents[0].normalized_content + " Correction.",
        "evidence_spans": (),
    }
)
from backend.rag.domain.article_document import normalized_content_hash  # noqa: E402

digest = normalized_content_hash(corrected.normalized_content)
corrected = corrected.model_copy(
    update={"content_hash": digest, "content_fingerprint": digest}
)
store.persist_article(corrected)
store.tombstone_article(documents[1].article_id)
report["after_edit_tombstone"] = [ask(service, t) for t in texts]

bundle = json.loads((ROOT / "fixtures/article_bundle.v1.json").read_text())
worker = build_runtime(url)
receipt = worker.ingest.execute(
    Article.model_validate_json(json.dumps(bundle["article"])), FIXTURE_CALLER
)
duplicate = worker.ingest.execute(
    Article.model_validate_json(json.dumps(bundle["article"])), FIXTURE_CALLER
)
from backend.rag.infra.telemetry.telemetry import RecordingTelemetry  # noqa: E402

run = worker.runner(RecordingTelemetry()).run(5)
report["worker"] = {
    "receipt": [receipt.current, receipt.job_created, duplicate.job_created],
    "same_job": receipt.job_id == duplicate.job_id,
    "outcomes": [o.kind.value for o in run.outcomes],
    "reconciled": run.reconciled,
}
references = adapter(PostgresSourceReferences)
token = references.issue(FIXTURE_CALLER, documents[3].evidence_spans[0])
resolved = references.resolve(token)
report["source_ref"] = {
    "resolved": None
    if resolved is None
    else [resolved.principal_id, resolved.scope_id, resolved.span.model_dump(mode="json")],
    "unknown": references.resolve("not-a-token") is None,
}

VOLATILE = {
    "job_id", "chunk_id", "created_at", "updated_at", "published_at", "applied_at",
    "next_attempt_at", "lease_expires_at", "indexed_at", "tombstoned_at",
    "token_sha256", "expires_at", "publication_id", "search_vector",
}
KEYS = {
    "rag_article_documents": "article_id, content_hash",
    "rag_article_current": "article_id",
    "rag_enrichment_jobs": "article_id, content_hash, enrichment_version",
    "rag_index_generations": "generation",
    "rag_chunks": "article_id, content_hash, generation, chunk_index",
    "rag_enrichment_versions": "generation",
    "rag_enrichment_publications": "index_generation, claim_generation",
    "rag_source_references": "article_id, span_id",
    "rag_schema_migrations": "filename",
}
CLOCK_KEYS = {"ingested_at", "processed_at", "enriched_at"}


def _mask(value):  # noqa: ANN001, ANN202
    if isinstance(value, dict):
        return {k: ("<clock>" if k in CLOCK_KEYS else v) for k, v in value.items()}
    return value


tables: dict[str, object] = {}
plans: dict[str, str] = {}
with psycopg.connect(url) as connection:
    for table, key in KEYS.items():
        columns = [
            r[0]
            for r in connection.execute(
                """SELECT column_name FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s
                ORDER BY ordinal_position""",
                (table,),
            ).fetchall()
            if r[0] not in VOLATILE
        ]
        if table == "rag_schema_migrations":
            columns = ["filename", "checksum"]
        rows = connection.execute(
            f"SELECT {', '.join(columns)} FROM {table} ORDER BY {key}"  # noqa: S608
        ).fetchall()
        tables[table] = [[str(_mask(v)) for v in r] for r in rows]
    connection.execute("ANALYZE")
    connection.commit()
    scope = pr.scope_params(pr.RetrievalScope((), None, None, None))
    params = {
        "lexical": (pr.LEXICAL_SQL, {**scope, "query": texts[0], "stopwords": [],
                    "max_terms": 16, "article_pool": 50, "scan": 8, "per_article": 3,
                    "metadata_cap": 400, "size": 800, "step": 600}),
        "vector_exact": (pr.VECTOR_EXACT_SQL, {**scope, **pr.identity_params(
            pr.EmbeddingIdentity()), "embedding": pr.vector_literal([0.01] * 1536),
            "limit": 20}),
        "current": (pr.CURRENT_SQL, {"ids": [d.article_id for d in documents[:5]]}),
        "eligibility": (pr.ELIGIBILITY_SQL, {**scope, "topics": [], "entities": [],
                        "cap": 400}),
    }
    for name, (query, bound) in params.items():
        lines = connection.execute(
            "EXPLAIN (COSTS OFF) " + query, bound
        ).fetchall()
        plans[name] = "\n".join(str(line[0]) for line in lines)
report["tables"] = tables
report["plans"] = plans
out.write_text(json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False))
print("wrote", out, "impl", report["implementation"])
