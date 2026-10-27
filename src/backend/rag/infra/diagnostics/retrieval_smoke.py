"""Deterministic T5 smoke + fixture measurement through ``RetrieveEvidence``.

Loads the 30-article fixture corpus into an empty isolated RAG database,
runs the named smoke cases (lexical_only, filter_unavailable, zero_hit,
happy, rerank_degraded) and measures lexical-only, vector-only, RRF and
RRF+rerank on the 10 fixture questions. Output contains IDs, statuses and
metrics only - never article text. Fake embeddings and a 30/10 fixture set
prove mechanics, not semantic quality or production latency.
"""

import json
import math
from collections.abc import Callable  # noqa: TC003 - runtime annotation in dict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import TypeAdapter

from backend.rag.application.ports.reranker import RerankerPort
from backend.rag.application.use_cases.index import EnsureGeneration
from backend.rag.application.use_cases.retrieve import (
    RetrievalOptions,
    RetrieveEvidence,
    bundled_retrieval_policy,
)
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import QueryControl, RetrievalResult
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings
from backend.rag.infra.llm.fixture_tokens import FixtureTokenCounter
from backend.rag.infra.postgres.alembic_runner import upgrade_head
from backend.rag.infra.postgres.engine import execute, rag_engine, scalar, transaction
from backend.rag.infra.postgres.publication import PostgresPublication, Publication
from backend.rag.infra.postgres.staging import PostgresStaging
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.retrieval.identity_reranker import IdentityReranker
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore
from backend.rag.infra.retrieval.scripted_reranker import (
    ScriptedReranker,
    failing_script,
)
from backend.shared.contracts import ArticleEnrichment

QUERY_CLOCK = datetime(2026, 4, 1, tzinfo=UTC)
NOT_READY = (24, 28)  # left FAILED so the corpus mixes READY and non-READY
CALLER = CallerScope(
    schema_version="caller_scope.v1",
    principal_id="fixture-analyst",
    scope_id="fixture-internal",
    policy_id="fixture-policy",
    policy_version="v1",
)
_FILE = TypeAdapter(dict[str, object])
_QUESTIONS = TypeAdapter(list[dict[str, object]])
_REFERENCES = TypeAdapter(list[dict[str, str]])


class SmokeDatabaseNotEmptyError(RuntimeError):
    """Smoke needs an empty, isolated RAG database."""

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("retrieval smoke needs an empty, isolated RAG database")


@dataclass(frozen=True, slots=True)
class Question:
    """Fixture question with expected article IDs (empty for abstain)."""

    question_id: str
    text: str
    expected: frozenset[UUID]


def _service(url: str, reranker: RerankerPort | None) -> RetrieveEvidence:
    return RetrieveEvidence(
        store=PostgresRetrievalStore(rag_engine(url)),
        embeddings=FakeEmbeddings(),
        embedding_identity=EmbeddingIdentity(),
        reranker=reranker,
        tokens=FixtureTokenCounter(),
        access=FixtureAccessPolicy(),
        policy=bundled_retrieval_policy(),
        clock=lambda: QUERY_CLOCK,
    )


def _query(text: str, **overrides: object) -> BriefingQuery:
    payload: dict[str, object] = {
        "schema_version": "briefing_query.v1",
        "query": text,
        "publication_time_basis": "source_published_at",
        "result_limit": 20,
        "context_budget": 12_000,
    }
    payload.update(overrides)
    return BriefingQuery.model_validate(payload)


def summarize(result: RetrievalResult) -> dict[str, object]:
    """Typed result summary without article text."""
    return {
        "status": result.status.value,
        "empty_reason": None if result.empty_reason is None else result.empty_reason,
        "retrieval_mode": result.retrieval_mode.value,
        "vector_branch_status": result.vector_branch_status.value,
        "rerank_status": result.rerank_status.value,
        "rerank_degrade_reason": result.rerank_degrade_reason,
        "coverage": {
            "status": result.coverage.status.value,
            "scoped": result.coverage.scoped_articles,
            "verifiable": result.coverage.verifiable_articles,
            "unverifiable": result.coverage.unverifiable_articles,
            "matching": result.coverage.matching_articles,
        },
        "provenance": {
            "policy_id": result.provenance.policy_id,
            "fusion_policy_id": result.provenance.fusion_policy_id,
            "index_version": result.provenance.index_version,
            "reranker": [
                result.provenance.reranker_id,
                result.provenance.reranker_version,
            ],
            "time_basis": result.provenance.publication_time_basis,
        },
        "articles": [
            {
                "rank": a.rank,
                "article_id": str(a.article_id),
                "basis": a.retrieval_basis.value,
                "vector_generation": a.vector_generation,
                "metadata": None
                if a.metadata is None
                else a.metadata.enrichment_version,
                "duplicates": [str(d) for d in a.duplicate_article_ids],
                "spans": [
                    [str(s.span.span_id), s.span.start_offset, s.span.end_offset]
                    for s in a.spans
                ],
            }
            for a in result.articles
        ],
        "context": {
            "tokens": result.context.token_count,
            "budget": result.context.token_budget,
            "tokenizer": result.context.tokenizer_id,
            "truncated_spans": result.context.truncated_spans,
        },
        "metrics": {
            "storage_calls": result.metrics.storage_calls,
            "embedding_calls": result.metrics.embedding_calls,
            "rerank_calls": result.metrics.rerank_calls,
            "suppressed_duplicates": result.metrics.suppressed_duplicates,
            "dropped_at_final_check": result.metrics.dropped_at_final_check,
            "stages": [
                [s.stage, round(s.latency_ms, 3), s.candidates_in, s.candidates_out]
                for s in result.metrics.stages
            ],
        },
    }


def _publish(url: str, document: ArticleDocument, metadata: ArticleEnrichment) -> None:
    """Real T4 primitives: enqueue -> claim -> stage -> fenced READY publish."""
    store = PostgresRagStore(rag_engine(url))
    job = store.enqueue_job(
        document.article_id,
        document.content_hash,
        metadata.enrichment_version,
        metadata.model_version,
        metadata.prompt_version,
    )
    claim = store.claim_job(job)
    if claim is None:
        msg = "fixture job could not be claimed"
        raise RuntimeError(msg)
    staged = EnsureGeneration(
        PostgresStaging(rag_engine(url)), FakeEmbeddings(), EmbeddingIdentity()
    ).execute(document)
    _ = PostgresPublication(rag_engine(url)).publish(
        Publication(job, claim, staged.generation, metadata)
    )


def load_fixture_corpus(url: str, fixtures: Path) -> list[ArticleDocument]:
    """Migrate and persist the 30 fixture ArticleDocuments (lexical only)."""
    _ = upgrade_head(url)
    with transaction(rag_engine(url), read_only=True) as connection:
        count = scalar(
            execute(connection, "SELECT count(*) FROM rag_article_documents")
        )
    if count != 0:
        raise SmokeDatabaseNotEmptyError
    store = PostgresRagStore(rag_engine(url))
    documents = [
        ArticleDocument.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted((fixtures / "articles").glob("*.json"))
    ]
    for document in documents:
        store.persist_article(document)
    return documents


def enrich_fixture_corpus(
    url: str, fixtures: Path, documents: list[ArticleDocument]
) -> None:
    """Publish fixture READY metadata; NOT_READY articles get FAILED jobs."""
    store = PostgresRagStore(rag_engine(url))
    for number, document in enumerate(documents, start=1):
        path = fixtures / f"enrichments/{number:02d}.json"
        metadata = ArticleEnrichment.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if number in NOT_READY:
            job = store.enqueue_job(
                document.article_id,
                document.content_hash,
                metadata.enrichment_version,
                metadata.model_version,
                metadata.prompt_version,
            )
            with transaction(rag_engine(url)) as connection:
                _ = execute(
                    connection,
                    """UPDATE rag_enrichment_jobs SET status = 'FAILED'
                    WHERE job_id = %s""",
                    (job,),
                )
            continue
        _publish(url, document, metadata)


def questions(fixtures: Path) -> list[Question]:
    """Fixture questions with expected article IDs."""
    raw = _FILE.validate_json((fixtures / "questions.v1.json").read_bytes())
    result: list[Question] = []
    for item in _QUESTIONS.validate_python(raw["questions"]):
        references = _REFERENCES.validate_python(item["expected_references"])
        result.append(
            Question(
                question_id=str(item["question_id"]),
                text=str(item["question"]),
                expected=frozenset(UUID(r["article_id"]) for r in references),
            )
        )
    return result


def _rank_metrics(
    ranked: list[UUID], expected: frozenset[UUID], k: int
) -> tuple[int, float, float]:
    hits = len(set(ranked[:k]) & expected)
    reciprocal = next(
        (1.0 / (i + 1) for i, item in enumerate(ranked) if item in expected), 0.0
    )
    dcg = sum(
        1.0 / math.log2(i + 2) for i, item in enumerate(ranked[:10]) if item in expected
    )
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(expected), 10)))
    return hits, reciprocal, (dcg / ideal if ideal else 0.0)


def measure_modes(url: str, items: list[Question], k: int) -> dict[str, object]:
    """Recall@k (with denominators), MRR and nDCG@10 per retrieval mode."""
    modes: dict[str, tuple[RetrievalOptions, RerankerPort | None]] = {
        "lexical_only": (RetrievalOptions(vector_enabled=False), None),
        "vector_only": (RetrievalOptions(lexical_enabled=False), None),
        "rrf": (RetrievalOptions(rerank_enabled=False), None),
        "rrf_rerank_identity": (RetrievalOptions(), IdentityReranker()),
    }
    report: dict[str, object] = {}
    answerable = [q for q in items if q.expected]
    for name, (options, reranker) in modes.items():
        retrieve = _service(url, reranker)
        found = 0
        denominator = 0
        mrr = 0.0
        ndcg = 0.0
        per_question: dict[str, list[str]] = {}
        for question in answerable:
            result = retrieve.execute(
                _query(question.text), CALLER, QueryControl(30.0), options
            )
            ranked = [a.article_id for a in result.articles]
            hits, reciprocal, normalized = _rank_metrics(ranked, question.expected, k)
            found += hits
            denominator += len(question.expected)
            mrr += reciprocal
            ndcg += normalized
            per_question[question.question_id] = [str(i) for i in ranked[:k]]
        count = len(answerable)
        report[name] = {
            f"recall_at_{k}": found / denominator if denominator else None,
            "recall_hits": found,
            "recall_denominator": denominator,
            "mrr": mrr / count if count else None,
            "ndcg_at_10": ndcg / count if count else None,
            "questions_scored": count,
            "questions_excluded_abstain": len(items) - count,
            "per_question_top_k": per_question,
        }
    return report


def run_smoke(url: str, fixtures: Path) -> dict[str, object]:
    """Run all smoke cases and the fixture measurement; return a JSON report."""
    documents = load_fixture_corpus(url, fixtures)
    question = questions(fixtures)[0].text
    identity_service = _service(url, IdentityReranker())
    cases: dict[str, Callable[[], RetrievalResult]] = {
        "lexical_only": lambda: identity_service.execute(
            _query(question), CALLER, QueryControl(30.0)
        ),
        "filter_unavailable": lambda: identity_service.execute(
            _query(question, topic_filters=("transportation",)),
            CALLER,
            QueryControl(30.0),
        ),
        "zero_hit": lambda: identity_service.execute(
            _query("zzzqqq xylophonequartz"),
            CALLER,
            QueryControl(30.0),
            RetrievalOptions(vector_enabled=False),
        ),
    }
    report: dict[str, object] = {
        "query_clock": QUERY_CLOCK.isoformat(),
        "policy_id": bundled_retrieval_policy().policy_id,
        "cases": {name: summarize(case()) for name, case in cases.items()},
    }
    enrich_fixture_corpus(url, fixtures, documents)
    degraded_service = _service(url, ScriptedReranker(failing_script))
    after: dict[str, Callable[[], RetrievalResult]] = {
        "happy": lambda: identity_service.execute(
            _query(question, topic_filters=("transportation",)),
            CALLER,
            QueryControl(30.0),
        ),
        "rerank_degraded": lambda: degraded_service.execute(
            _query(question), CALLER, QueryControl(30.0)
        ),
    }
    cases_report = report["cases"]
    if isinstance(cases_report, dict):
        for name, case in after.items():
            cases_report[name] = summarize(case())
    report["measurement"] = measure_modes(url, questions(fixtures), 20)
    return report


def dump(report: dict[str, object]) -> str:
    """Stable JSON rendering."""
    return json.dumps(report, indent=2, sort_keys=True, default=str)
