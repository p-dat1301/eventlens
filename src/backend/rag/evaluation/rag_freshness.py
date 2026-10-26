"""Freshness and concurrency scenario cases for ``rag_cases``."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from backend.rag.application.use_cases.inspect_source import SourceError
from backend.rag.composition import FIXTURE_CALLER
from backend.rag.domain.article_document import ArticleDocument, normalized_content_hash
from backend.rag.infra.retrieval.memory_retrieval import InMemoryRetrievalStore

from .corpus import EvalCorpus
from .metrics import Json, strs
from .rag_support import (
    Asked,
    answer_checks,
    ask,
    briefing_query,
    fake_model,
    hybrid_store,
    runtime,
)

Q1 = "When did East Pier close and reopen?"
HARBOR = UUID("00000000-0000-4000-8000-000000000001")
REOPEN = UUID("00000000-0000-4000-8000-000000000002")
type Pair = tuple[dict[str, Json], dict[str, Json]]


def _revised(document: ArticleDocument) -> ArticleDocument:
    content = document.normalized_content + " Update: East Pier closure extended."
    digest = normalized_content_hash(content)
    return document.model_copy(
        update={
            "raw_content": content,
            "normalized_content": content,
            "content_hash": digest,
            "content_fingerprint": digest,
            "evidence_spans": (),
        }
    )


def freshness_revision(corpus: EvalCorpus) -> Pair:
    """New revision served at once; old references stale; old metadata unused."""
    store = hybrid_store(corpus)
    composed, _ = runtime(store, fake_model())
    first = ask(composed, briefing_query(Q1))
    old_refs = [
        c.source_ref
        for c in (first.outcome.cited_sources if first.outcome else ())
        if c.citation.article_id == HARBOR
    ]
    revised = _revised(corpus.article(HARBOR))
    store.add(revised)  # persist new revision; no vectors/metadata until reindex
    second = ask(composed, briefing_query(Q1))
    hashes = sorted(
        {
            s.span.content_hash
            for s in (second.outcome.cited_sources if second.outcome else ())
            if s.citation.article_id == HARBOR
        }
    )
    stale: list[Json] = []
    for ref in old_refs:
        try:
            _ = composed.sources.execute(ref, FIXTURE_CALLER)
            stale.append("resolved")
        except SourceError as error:
            stale.append(type(error).__name__)
    observed: dict[str, Json] = {
        "cited_hashes_after_revision": strs(hashes),
        "old_reference_results": stale,
        "metadata_served_for_revision": HARBOR in store.metadata,
    }
    expected: dict[str, Json] = {
        "cited_hashes_after_revision": [revised.content_hash],
        "old_reference_results": strs(["SourceStaleError"] * len(old_refs)),
        "metadata_served_for_revision": False,
    }
    observed["old_references_checked"] = len(old_refs)
    expected["old_references_checked"] = 1
    return expected, observed


def freshness_tombstone(corpus: EvalCorpus) -> Pair:
    """A tombstoned article is never cited again."""
    store = hybrid_store(corpus)
    composed, _ = runtime(store, fake_model())
    before = ask(composed, briefing_query(Q1))
    store.tombstone(REOPEN)
    after = ask(composed, briefing_query(Q1))
    observed: dict[str, Json] = {
        "cited_before": _cites(before, REOPEN),
        "cited_after": _cites(after, REOPEN),
    }
    return {"cited_before": True, "cited_after": False}, observed


def _cites(asked: Asked, article_id: UUID) -> bool:
    sources = asked.outcome.cited_sources if asked.outcome else ()
    return any(s.citation.article_id == article_id for s in sources)


def _fingerprint(asked: Asked, store: InMemoryRetrievalStore) -> dict[str, Json]:
    if asked.outcome is None:
        return {"error_code": asked.error_code}
    citations = asked.outcome.result.answer.citations
    return {
        "route": asked.outcome.route.value,
        "cited": strs(sorted(str(c.article_id) for c in citations)),
        **answer_checks(asked.outcome, dict(store.documents)),
    }


def concurrency_isolation(corpus: EvalCorpus) -> Pair:
    """10 questions concurrently on one runtime equal the sequential results."""
    store = hybrid_store(corpus)
    composed, _ = runtime(store, fake_model())
    texts = [q.text for q in corpus.questions]

    def run(text: str) -> dict[str, Json]:
        return _fingerprint(ask(composed, briefing_query(text)), store)

    sequential = [run(text) for text in texts]
    with ThreadPoolExecutor(max_workers=len(texts)) as pool:
        concurrent = list(pool.map(run, texts))
    fresh_store = hybrid_store(corpus)
    fresh, _ = runtime(fresh_store, fake_model())
    first = _fingerprint(ask(fresh, briefing_query(texts[0])), fresh_store)
    observed: dict[str, Json] = {
        "requests": len(texts),
        "identical_to_sequential": sum(
            a == b for a, b in zip(sequential, concurrent, strict=True)
        ),
        "fresh_runtime_matches": sequential[0] == first,
    }
    expected: dict[str, Json] = {
        "requests": len(texts),
        "identical_to_sequential": len(texts),
        "fresh_runtime_matches": True,
    }
    return expected, observed
