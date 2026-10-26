"""``make rag-eval``: persist -> lexical -> enrich -> reindex -> hybrid, one graph.

One in-memory store and ONE composed runtime (one compiled briefing
StateGraph, one fake model, one budget) answer the 10 fixture questions twice:
first with the corpus persisted but not enriched (lexical-only), then after the
fake enricher ran through the compiled ingestion graph and READY metadata plus
fake vectors were reindexed (hybrid). Only the index state differs between the
two modes. Scenario cases live in ``rag_cases``.
"""

from collections.abc import Callable
from uuid import UUID

from backend.rag.application.ports.synthesis import SynthesisRequest
from backend.rag.application.synthesis_policies import bundled_synthesis_policy
from backend.rag.application.use_cases.briefing import BriefingOutcome
from backend.rag.application.use_cases.retrieve import bundled_retrieval_policy
from backend.rag.composition import BriefingRuntime, compose_briefing
from backend.rag.infra.llm.fake_enricher import FakeArticleEnricher
from backend.rag.infra.retrieval.memory_retrieval import InMemoryRetrievalStore

from .corpus import EVAL_CLOCK, EvalCorpus, EvalQuestion, corpus_versions
from .enrichment_eval import TOKENIZER
from .enrichment_world import EnrichmentWorld
from .guards import no_network
from .metrics import Json, digest, latency_summary, strs
from .rag_cases import run_cases
from .rag_summary import K_VALUES, summarize
from .rag_support import (
    Asked,
    CountingModel,
    answer_checks,
    ask,
    briefing_query,
    fake_model,
    ids,
    reindex,
    runtime,
)
from .scope import ScopedRetrievalStore, scope_violations

OVERHEAD_DEFINITION = (
    "use-case wall time minus retrieval stage latencies minus synthesis total "
    "latency (graph orchestration + admission + reference issuing)"
)
LIMITATIONS = (
    (
        "10 smoke questions on a 30-article synthetic corpus; fake synthesizer "
        "copies the first sent span per article; fake embeddings use a fixed "
        "distance. Results prove wiring/accounting, not answer quality."
    ),
    (
        "Each question runs inside its fixture allowed_article_ids scope "
        "(eval-level ScopedRetrievalStore; G1-G3 have no per-article ACL). For "
        "every answerable question allowed == expected articles, so Recall@k "
        "in ``modes`` is near-trivial. The ranking comparison lives in "
        "``ranking``: one analyst scope with 29 permitted articles "
        "(distractors), restricted article 014 excluded."
    ),
    (
        "Gold spans in fixtures v1 cover each expected article's full text, so "
        "span metrics equal article metrics and cannot judge passage choice."
    ),
    (
        "In-memory retrieval store approximates PostgreSQL FTS; the DB suite "
        "(make rag-db-test) carries real PostgreSQL/pgvector evidence."
    ),
    (
        "Latency figures are wall-clock on the eval machine with fake adapters, "
        "not a production SLO; the 8 s p95 target stays unmeasured."
    ),
)


def _span_hits(outcome: BriefingOutcome, question: EvalQuestion) -> dict[str, Json]:
    """Citation spans against gold fixture spans (exact and overlapping)."""
    sent = {
        span.span_id: (span.article_id, span.start_offset, span.end_offset)
        for document in outcome.result.answer.source_documents
        for span in document.evidence_spans
    }
    cited = [sent[c.evidence_span_id] for c in outcome.result.answer.citations]
    gold = question.expected_spans

    def overlaps(a: tuple[UUID, int, int], b: tuple[UUID, int, int]) -> bool:
        return a[0] == b[0] and a[1] < b[2] and b[1] < a[2]

    return {
        "citations_gold_span_exact": sum(c in gold for c in cited),
        "citations_gold_span_overlap": sum(
            any(overlaps(c, g) for g in gold) for c in cited
        ),
        "gold_spans": len(gold),
        "gold_spans_covered": sum(any(overlaps(c, g) for c in cited) for g in gold),
    }


def _record(
    asked: Asked,
    question: EvalQuestion,
    store: InMemoryRetrievalStore,
    requests: list[SynthesisRequest],
    allowed: frozenset[UUID],
) -> dict[str, Json]:
    expected = set(question.expected_articles)
    outcome = asked.outcome
    violations = scope_violations(outcome, requests, allowed)
    if outcome is None:
        return {
            "route": "error",
            "error_code": asked.error_code,
            "scope_violations": violations,
        }
    ranked = [a.article_id for a in outcome.retrieval.articles]
    answer = outcome.result.answer
    cited = sorted({str(c.article_id) for c in answer.citations})
    usage = outcome.result.usage
    record: dict[str, Json] = {
        "route": outcome.route.value,
        "retrieval_mode": outcome.retrieval.retrieval_mode.value,
        "vector_branch_status": outcome.retrieval.vector_branch_status.value,
        "top_10": ids(ranked[:10]),
        "cited_articles": strs(cited),
        "cited_expected": len({UUID(c) for c in cited} & expected),
        "uncertainty_categories": strs(
            sorted({u.category for u in answer.uncertainties})
        ),
        "temporal_claims": sum(c.temporal_claim is not None for c in answer.claims),
        "time_basis_label": question.time_basis,
        "query_time_basis": outcome.retrieval.provenance.publication_time_basis,
        "model_calls": usage.model_calls,
        "input_tokens_counted": usage.input_tokens_counted,
        "input_tokens_charged": usage.input_tokens_charged,
        "output_tokens_charged": usage.output_tokens_charged,
        "usage_status": usage.usage_status.value,
        "cost_charged": usage.cost_charged,
        "cost_unit": usage.cost_unit,
        **answer_checks(outcome, dict(store.documents)),
        **_span_hits(outcome, question),
        "scope_violations": violations,
    }
    for k in K_VALUES:
        record[f"recall_at_{k}"] = (
            {"hits": len(set(ranked[:k]) & expected), "expected": len(expected)}
            if expected
            else None
        )
    record["ranks_of_expected"] = [
        ranked.index(a) + 1 if a in ranked else None for a in sorted(expected, key=str)
    ]
    return record


def _timing(asked: Asked) -> tuple[float, float | None]:
    """(wall ms, orchestration overhead ms outside retrieval/synthesis stages)."""
    outcome = asked.outcome
    if outcome is None:
        return asked.wall_ms, None
    inner = sum(s.latency_ms for s in outcome.retrieval.metrics.stages)
    inner += outcome.result.metrics.total_latency_ms
    return asked.wall_ms, max(0.0, asked.wall_ms - inner)


type ScopeFor = Callable[[EvalQuestion], frozenset[UUID]]


def question_scope(question: EvalQuestion) -> frozenset[UUID]:
    """The question's own fixture ``allowed_article_ids``."""
    return frozenset(question.allowed_articles)


def _pass(  # noqa: PLR0913
    composed: BriefingRuntime,
    corpus: EvalCorpus,
    scoped: ScopedRetrievalStore,
    model: CountingModel,
    *,
    repeats: int,
    scope_for: ScopeFor,
) -> tuple[dict[str, dict[str, Json]], bool, list[float], list[float]]:
    records: dict[str, dict[str, Json]] = {}
    consistent = True
    walls: list[float] = []
    overheads: list[float] = []
    for _ in range(repeats):
        for question in corpus.questions:
            allowed = scope_for(question)
            scoped.allowed = allowed
            first = len(model.requests)
            asked = ask(composed, briefing_query(question.text))
            requests = model.requests[first:]
            record = _record(asked, question, scoped.inner, requests, allowed)
            previous = records.setdefault(question.question_id, record)
            consistent = consistent and previous == record
            wall, overhead = _timing(asked)
            walls.append(wall)
            if overhead is not None:
                overheads.append(overhead)
    scoped.allowed = None
    return records, consistent, walls, overheads


def _safety(composed: BriefingRuntime, network_attempts: int) -> dict[str, Json]:
    default = compose_briefing(InMemoryRetrievalStore())
    model = composed.use_case.services.synthesizer.model
    compiled = getattr(default.graph, "_compiled", None)
    return {
        "default_model": type(default.use_case.services.synthesizer.model).__name__,
        "activation_mode": default.use_case.activation.mode,
        "graph_checkpointer": repr(getattr(compiled, "checkpointer", "missing")),
        "eval_model_identity": model.identity.model_id,
        "network_connect_attempts": network_attempts,
    }


def evaluate(
    corpus: EvalCorpus, repeats: int = 3
) -> tuple[dict[str, Json], dict[str, Json]]:
    """Return (deterministic report, measured-environment report)."""
    with no_network() as attempts:
        store = InMemoryRetrievalStore()
        for document in corpus.articles:
            store.add(document)  # persist: lexical search available at once
        scoped = ScopedRetrievalStore(store)
        model = fake_model()
        composed, _ = runtime(scoped, model)

        def ranking_scope(_: EvalQuestion) -> frozenset[UUID]:
            return corpus.ranking_scope

        lexical, lexical_ok, lexical_wall, lexical_overhead = _pass(
            composed, corpus, scoped, model, repeats=repeats, scope_for=question_scope
        )
        rank_lex, rank_lex_ok, rank_lex_wall, rank_lex_overhead = _pass(
            composed, corpus, scoped, model, repeats=repeats, scope_for=ranking_scope
        )
        world = EnrichmentWorld()
        world.persist(*corpus.articles)
        enricher = FakeArticleEnricher()
        enrichment = world.drain(enricher)
        reindexed = reindex(store, dict(store.documents), world.published())
        hybrid, hybrid_ok, hybrid_wall, hybrid_overhead = _pass(
            composed, corpus, scoped, model, repeats=repeats, scope_for=question_scope
        )
        rank_hyb, rank_hyb_ok, rank_hyb_wall, rank_hyb_overhead = _pass(
            composed, corpus, scoped, model, repeats=repeats, scope_for=ranking_scope
        )
        cases, case_latency = run_cases(corpus)
        semantic = cases.pop("semantic_support")
        safety = _safety(composed, attempts.count)
    deterministic: dict[str, Json] = {
        "schema_version": "rag_eval_report.v1",
        "versions": {
            **corpus_versions(corpus.root),
            "retrieval_policy": bundled_retrieval_policy().policy_id,
            "synthesis_policy": bundled_synthesis_policy().policy_id,
            "chat_model": safety["eval_model_identity"],
            "tokenizer": TOKENIZER,
            "enricher": f"{enricher.model_version}/{enricher.prompt_version}",
            "clock": EVAL_CLOCK.isoformat(),
            "budget": {"context_budget": 12_000, "result_limit": 20},
        },
        "sample": {
            "articles": len(corpus.articles),
            "questions": len(corpus.questions),
            "repeats_per_mode": repeats,
            "enrichment_jobs": len(enrichment),
            "reindexed_articles": reindexed,
            "repeat_consistent": {
                "lexical": lexical_ok,
                "hybrid": hybrid_ok,
                "ranking_lexical": rank_lex_ok,
                "ranking_hybrid": rank_hyb_ok,
            },
            "ranking_scope_articles": len(corpus.ranking_scope),
            "ranking_scope_restricted_excluded": ids(
                sorted(corpus.restricted, key=str)
            ),
        },
        "modes": {
            "lexical": {
                "summary": summarize(lexical, corpus),
                "per_question": dict(lexical),
            },
            "hybrid": {
                "summary": summarize(hybrid, corpus),
                "per_question": dict(hybrid),
            },
        },
        # Ranking comparison: same graph, questions, model and budget, but
        # every question searches the full permitted analyst scope
        # (distractors included, restricted articles excluded).
        "ranking": {
            "lexical": {
                "summary": summarize(rank_lex, corpus),
                "per_question": dict(rank_lex),
            },
            "hybrid": {
                "summary": summarize(rank_hyb, corpus),
                "per_question": dict(rank_hyb),
            },
        },
        "cases": cases,
        "semantic_vs_structural": semantic,
        "safety": safety,
        "limitations": list(LIMITATIONS),
    }
    deterministic["report_digest"] = digest(deterministic)
    measured: dict[str, Json] = {
        "status": "measured on this machine; not reproducible; not a production SLO",
        "lexical": {
            "end_to_end": latency_summary(lexical_wall),
            "graph_overhead": latency_summary(lexical_overhead),
            "execution_errors": len(lexical_wall) - len(lexical_overhead),
        },
        "hybrid": {
            "end_to_end": latency_summary(hybrid_wall),
            "graph_overhead": latency_summary(hybrid_overhead),
            "execution_errors": len(hybrid_wall) - len(hybrid_overhead),
        },
        "ranking": {
            "lexical": {
                "end_to_end": latency_summary(rank_lex_wall),
                "graph_overhead": latency_summary(rank_lex_overhead),
                "execution_errors": len(rank_lex_wall) - len(rank_lex_overhead),
            },
            "hybrid": {
                "end_to_end": latency_summary(rank_hyb_wall),
                "graph_overhead": latency_summary(rank_hyb_overhead),
                "execution_errors": len(rank_hyb_wall) - len(rank_hyb_overhead),
            },
        },
        "case_latency": latency_summary(case_latency),
        "graph_overhead_definition": OVERHEAD_DEFINITION,
    }
    return deterministic, measured
