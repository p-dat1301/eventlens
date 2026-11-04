"""Search-selection benchmark orchestration over ports (no infra imports).

Phase 1 indexes one corpus snapshot into every engine and runs A-D. The best
hybrid is selected on the dev split only (nDCG@10, then Recall@10, then name),
then phase 2 runs it with and without the reranker (E). All numbers are
reported on dev, test and reviewed-test separately; test is never used to
choose analyzers, models, fusion or reranking.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar
from uuid import UUID

from backend.rag.application.ports.access import AccessPolicy
from backend.rag.application.ports.reranker import RerankerPort
from backend.rag.application.ports.retrieval import RetrievalStore
from backend.rag.application.ports.search_index import (
    ChunkLexicalIndex,
    ChunkVectorIndex,
    TextEmbedder,
)
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.chunking import ChunkingPolicy, chunk_document
from backend.rag.domain.retrieval import ChunkRef, RetrievalPolicy
from backend.rag.domain.search_index import EmbedderIdentity, IndexedChunk
from backend.rag.evaluation.search_bench.candidates import corpus_digest
from backend.rag.evaluation.search_bench.labels import QuestionSet, SearchQuestion
from backend.rag.evaluation.search_bench.metrics import percentile
from backend.rag.evaluation.search_bench.runner import (
    WARMUP_QUERIES,
    ArticleFacts,
    SystemRun,
    breakdown,
    compare,
    headline,
    per_question,
    run_benchmark,
    subsets,
    summarize,
)
from backend.rag.evaluation.search_bench.systems import (
    Bm25System,
    FtsSystem,
    RerankSystem,
    RetrievalSystem,
    RrfSystem,
    SemanticSystem,
)

REPORT_SCHEMA = "search-selection-report.v1"
BOOTSTRAP_SEED = 20261027


def build_chunks(
    documents: Sequence[ArticleDocument], chunking: ChunkingPolicy
) -> list[IndexedChunk]:
    """T3 chunk windows of every current revision (one shared set)."""
    return [
        IndexedChunk(
            chunk=ChunkRef(
                article_id=document.article_id,
                content_hash=document.content_hash,
                chunker_version=chunking.version,
                chunk_index=chunk.index,
                start_offset=chunk.start_offset,
                end_offset=chunk.end_offset,
            ),
            title=document.title,
            content=chunk.content,
            source_domain=document.source_domain,
            source_published_at=document.source_published_at,
        )
        for document in documents
        for chunk in chunk_document(document, chunking)
    ]


@dataclass(slots=True)
class Engines:
    """Adapters behind ports; the CLI wires real or fake implementations.

    ``release`` frees one embedder's model after its documents and queries are
    embedded, so large models never share memory (``CachedQueries`` replaces it).
    """

    store: RetrievalStore
    lexical: Mapping[str, ChunkLexicalIndex]
    embedders: dict[str, TextEmbedder]
    vectors: ChunkVectorIndex
    reranker: RerankerPort | None
    release: Callable[[str], None] = field(default=lambda _name: None)
    index_stats: dict[str, dict[str, object]] = field(default_factory=dict)


class QueryNotPrecomputedError(KeyError):
    """A query was not embedded during indexing (benchmark wiring bug)."""

    code: ClassVar[str] = "query_not_precomputed"


@dataclass(frozen=True, slots=True)
class CachedQueries:
    """``TextEmbedder`` serving query vectors embedded (and timed) up front.

    Query embedding latency is measured per query during indexing and reported
    in ``index_stats[...]["query_embed_ms"]``; system latencies of C/D/E then
    cover search, fusion and reranking only.
    """

    model_identity: EmbedderIdentity
    vectors: Mapping[str, tuple[float, ...]]

    @property
    def identity(self) -> EmbedderIdentity:
        """Identity of the model that produced the vectors."""
        return self.model_identity

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Documents are never embedded after indexing."""
        raise QueryNotPrecomputedError(len(texts))

    def embed_queries(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Look up precomputed vectors."""
        try:
            return tuple(self.vectors[text] for text in texts)
        except KeyError as error:
            raise QueryNotPrecomputedError(str(error)) from None


def index_corpus(
    engines: Engines,
    chunks: Sequence[IndexedChunk],
    queries: Sequence[str],
    clock: Callable[[], float] = time.perf_counter,
) -> None:
    """Rebuild every index from the same chunks; embed queries; time both."""
    for name, index in engines.lexical.items():
        started = clock()
        index.rebuild(chunks)
        engines.index_stats[name] = {
            "chunks": len(chunks),
            "index_seconds": clock() - started,
            "size_bytes": index.size_bytes(),
        }
    texts = [item.text for item in chunks]
    unique = list(dict.fromkeys(queries))
    for name, embedder in list(engines.embedders.items()):
        started = clock()
        vectors = embedder.embed_documents(texts)
        embedded = clock()
        engines.vectors.rebuild(embedder.identity, chunks, vectors)
        loaded = clock()
        for query in unique[:WARMUP_QUERIES]:
            _ = embedder.embed_queries([query])
        cached: dict[str, tuple[float, ...]] = {}
        latencies: list[float] = []
        for query in unique:
            begin = clock()
            (cached[query],) = embedder.embed_queries([query])
            latencies.append((clock() - begin) * 1000.0)
        engines.index_stats[name] = {
            "chunks": len(chunks),
            "embed_seconds": embedded - started,
            "load_seconds": loaded - embedded,
            "size_bytes": engines.vectors.size_bytes(embedder.identity),
            "query_embed_ms": {
                "samples": len(latencies),
                "p50": percentile(latencies, 50),
                "p95": percentile(latencies, 95),
            },
        }
        identity = embedder.identity
        engines.release(name)
        engines.embedders[name] = CachedQueries(identity, cached)


def phase_one(
    engines: Engines, policy: RetrievalPolicy, chunking: ChunkingPolicy, now: datetime
) -> list[RetrievalSystem]:
    """A, B (per analyzer), C (per embedder), D (every lexical x semantic)."""
    fts: RetrievalSystem = FtsSystem(engines.store, policy, chunking, now)
    lexical: list[RetrievalSystem] = [fts]
    lexical.extend(
        Bm25System(index, name=f"B_bm25[{name}]")
        for name, index in engines.lexical.items()
    )
    semantic: list[RetrievalSystem] = [
        SemanticSystem(embedder, engines.vectors, name=f"C_sem[{name}]")
        for name, embedder in engines.embedders.items()
    ]
    hybrids: list[RetrievalSystem] = [
        RrfSystem(left, right, name=f"D_rrf[{left.name}+{right.name}]")
        for left in lexical
        for right in semantic
    ]
    return [*lexical, *semantic, *hybrids]


def _dev_key(run: SystemRun, dev: Sequence[SearchQuestion]) -> tuple[float, float, str]:
    ndcg, recall, _ = headline(run, dev)
    return (-ndcg, -recall, run.name)


def best_hybrid(
    runs: Sequence[SystemRun],
    systems: Sequence[RetrievalSystem],
    question_set: QuestionSet,
) -> RetrievalSystem | None:
    """Dev-only selection among the D systems."""
    dev = subsets(question_set)["dev"]
    hybrids = [run for run in runs if run.name.startswith("D_")]
    if not hybrids:
        return None
    chosen = min(hybrids, key=lambda run: _dev_key(run, dev))
    return next(system for system in systems if system.name == chosen.name)


@dataclass(frozen=True, slots=True)
class BenchInputs:
    """Everything a run depends on (recorded in the report)."""

    question_set: QuestionSet
    documents: tuple[ArticleDocument, ...]
    facts: Mapping[UUID, ArticleFacts]
    policy: RetrievalPolicy
    chunking: ChunkingPolicy
    now: datetime
    repeats: int
    environment: Mapping[str, object]


def run_search_selection(
    engines: Engines,
    inputs: BenchInputs,
    access: AccessPolicy,
    caller: CallerScope,
) -> dict[str, object]:
    """Index, run phase 1, select on dev, run phase 2, build the report."""
    chunks = build_chunks(inputs.documents, inputs.chunking)
    digest = corpus_digest([(d.article_id, d.content_hash) for d in inputs.documents])
    if digest != inputs.question_set.corpus_digest:
        msg = "question set was labelled on a different corpus snapshot"
        raise ValueError(msg)
    index_corpus(engines, chunks, [q.question for q in inputs.question_set.questions])
    systems = phase_one(engines, inputs.policy, inputs.chunking, inputs.now)
    runs = run_benchmark(
        systems, inputs.question_set, access, caller, repeats=inputs.repeats
    )
    chosen = best_hybrid(runs, systems, inputs.question_set)
    if chosen is not None and engines.reranker is not None:
        texts = {item.chunk.chunk_id: item.text for item in chunks}
        rerank = RerankSystem(
            chosen,
            engines.reranker,
            lambda ids: {i: texts[i] for i in ids},
            name=f"E_rerank[{chosen.name}]",
        )
        runs.extend(
            run_benchmark(
                [rerank], inputs.question_set, access, caller, repeats=inputs.repeats
            )
        )
    return build_report(
        runs,
        inputs=inputs,
        engines=engines,
        digest=digest,
        chunk_count=len(chunks),
        chosen=chosen,
    )


def build_report(  # noqa: PLR0913 - one report, many inputs
    runs: Sequence[SystemRun],
    *,
    inputs: BenchInputs,
    engines: Engines,
    digest: str,
    chunk_count: int,
    chosen: RetrievalSystem | None,
) -> dict[str, object]:
    """Text-free report: config, per-split summaries, breakdowns, per-question."""
    groups = subsets(inputs.question_set)
    by_name = {run.name: run for run in runs}
    comparisons: list[dict[str, object]] = []
    if chosen is not None:
        reranked = by_name.get(f"E_rerank[{chosen.name}]")
        pairs = [(by_name[chosen.name], by_name["A_fts"])]
        if reranked is not None:
            pairs.append((reranked, by_name[chosen.name]))
            pairs.append((reranked, by_name["A_fts"]))
        best_bm25 = [r for r in runs if r.name.startswith("B_")]
        if best_bm25:
            top = min(best_bm25, key=lambda r: _dev_key(r, groups["dev"]))
            pairs.append((top, by_name["A_fts"]))
        for split in ("test", "reviewed_test"):
            comparisons.extend(
                {"split": split, **compare(left, right, groups[split], BOOTSTRAP_SEED)}
                for left, right in pairs
            )
    status_counts: dict[str, int] = {}
    for question in inputs.question_set.questions:
        status_counts[question.label_status] = (
            status_counts.get(question.label_status, 0) + 1
        )
    return {
        "schema": REPORT_SCHEMA,
        "environment": dict(inputs.environment),
        "corpus": {
            "export_id": inputs.question_set.corpus_export_id,
            "digest": digest,
            "documents": len(inputs.documents),
            "chunks": chunk_count,
            "chunker_version": inputs.chunking.version,
        },
        "questions": {
            "total": len(inputs.question_set.questions),
            "label_status": status_counts,
            "split_seed": inputs.question_set.split_seed,
            "subset_sizes": {name: len(items) for name, items in groups.items()},
        },
        "run": {
            "repeats": inputs.repeats,
            "query_clock": inputs.now.isoformat(),
            "retrieval_policy": inputs.policy.policy_id,
            "selected_on_dev": None if chosen is None else chosen.name,
            "bootstrap_seed": BOOTSTRAP_SEED,
        },
        "index_stats": engines.index_stats,
        "systems": [
            {
                "name": run.name,
                "config": run.description,
                "summary": {
                    split: summarize(run, groups[split])
                    for split in ("dev", "test", "all", "reviewed_test")
                },
                "breakdown_test": breakdown(run, groups["test"], inputs.facts),
            }
            for run in runs
        ],
        "comparisons": comparisons,
        "per_question": {run.name: per_question(run, groups["all"]) for run in runs},
    }
