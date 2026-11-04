"""Query an imported handoff batch through the production briefing path.

Outer-ring diagnostic (like ``search_fixture_corpus.py``): for a few labelled
questions it runs briefing retrieval with Elasticsearch candidates (no
PostgreSQL fallback), opens the first cited source in the source viewer and
checks that the excerpt is the exact NFC slice of the stored revision and that
the cited revision maps back to its upstream ``article_version_id``. Prints a
text-free JSON summary (IDs, offsets, booleans).

    uv run python scripts/rag/handoff_probe.py DB_URL ES_URL QUESTIONS_JSON [N]
"""

import json
import sys
from pathlib import Path

from backend.rag.application.use_cases.search_candidates import IndexedCandidateSource
from backend.rag.composition import FIXTURE_CALLER, compose_briefing
from backend.rag.domain.query import BriefingQuery
from backend.rag.infra.elasticsearch.client import ElasticsearchClient
from backend.rag.infra.elasticsearch.indexes import ElasticsearchSearchIndex
from backend.rag.infra.embeddings.fake_bge_m3 import FakeBgeM3Embeddings
from backend.rag.infra.postgres.engine import rag_engine
from backend.rag.infra.postgres.search_platform import PostgresSearchGenerations
from backend.rag.infra.postgres.source_versions import PostgresSourceVersions
from backend.rag.infra.repositories.postgres_source_references import (
    PostgresSourceReferences,
)
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore

DEFAULT_QUESTIONS = 8
GOLD = 2


def main(db_url: str, es_url: str, questions_path: str, limit: int) -> int:
    """Probe ``limit`` unfiltered answerable questions; exit 1 on any failure."""
    engine = rag_engine(db_url)
    briefing = compose_briefing(
        PostgresRetrievalStore(engine),
        references=PostgresSourceReferences(engine),
        candidates=IndexedCandidateSource(
            index=ElasticsearchSearchIndex(ElasticsearchClient(es_url)),
            embedder=FakeBgeM3Embeddings(),
            generations=PostgresSearchGenerations(engine).active,
        ),
        fallback_to_store=False,
    )
    versions = PostgresSourceVersions(engine)
    payload = json.loads(Path(questions_path).read_text(encoding="utf-8"))
    picked = [
        q
        for q in payload["questions"]
        if q["answerable"]
        and not q["filters"]["source_domains"]
        and q["filters"]["published_after"] is None
    ][:limit]
    rows: list[dict[str, object]] = []
    for question in picked:
        outcome = briefing.use_case.execute(
            BriefingQuery.model_validate(
                {
                    "schema_version": "briefing_query.v1",
                    "query": question["question"],
                    "publication_time_basis": "source_published_at",
                    "result_limit": 5,
                    "context_budget": 12_000,
                }
            )
        )
        gold = {g["article_id"] for g in question["gold"] if g["grade"] == GOLD}
        articles = [str(a.article_id) for a in outcome.retrieval.articles]
        row: dict[str, object] = {
            "question_id": question["qid"],
            "search_backend": outcome.retrieval.provenance.search_backend,
            "gold_in_top5": bool(gold & set(articles)),
            "cited": len(outcome.cited_sources),
        }
        if outcome.cited_sources:
            cited = outcome.cited_sources[0]
            view = briefing.sources.execute(cited.source_ref, FIXTURE_CALLER)
            start, end = view.span.start_offset, view.span.end_offset
            provenance = versions.provenance_for((cited.span.article_id,))
            row |= {
                "cited_article_id": str(cited.span.article_id),
                "span": [start, end],
                "excerpt_is_exact_slice": view.span.excerpt
                == view.normalized_content[start:end],
                "article_version_ids": [str(p.article_version_id) for p in provenance],
                "revision_matches_provenance": any(
                    p.rag_content_hash == cited.span.content_hash for p in provenance
                ),
            }
        rows.append(row)
    ok = all(
        r["search_backend"] == "elasticsearch"
        and r.get("excerpt_is_exact_slice", True)
        and r.get("revision_matches_provenance", True)
        for r in rows
    )
    print(json.dumps({"ok": ok, "questions": rows}, indent=1, sort_keys=True))  # noqa: T201
    return 0 if ok and rows else 1


if __name__ == "__main__":
    arguments = sys.argv[1:]
    count = int(arguments[3]) if len(arguments) > 3 else DEFAULT_QUESTIONS  # noqa: PLR2004
    sys.exit(main(arguments[0], arguments[1], arguments[2], count))
