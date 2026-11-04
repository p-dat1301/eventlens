"""Build benchmark configurations from option names (offline only).

Names: ``memory-{bm25,semantic,hybrid}`` (in-process, CI-safe),
``es-{bm25,semantic,hybrid,hybrid-rerank}`` (needs an Elasticsearch URL) and
``postgres-lexical`` (needs an isolated database; built by the CLI). Real
models load only from the local Hugging Face cache with a pinned revision.

An index configuration may name its chunker: ``es-bm25@codepoint-1200-200.v1``
(otherwise ``SystemOptions.chunker``). Only the chunker changes; analyzer,
boosts, filters and depths stay identical, so two names differing only in
the suffix isolate the effect of chunking. ``postgres-lexical`` is the
production branch and only exists with ``codepoint-1200-200.v1``.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from backend.rag.application.ports.embeddings import BatchEmbeddingPort
from backend.rag.application.ports.retrieval import RetrievalStore
from backend.rag.application.retrieval_profiles import bundled_tuning
from backend.rag.application.use_cases.search_projection import ProjectionChunker
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.chunking import DEFAULT_POLICY
from backend.rag.domain.paragraph_chunking import DEFAULT_PARAGRAPH_POLICY
from backend.rag.domain.retrieval_tuning import InvalidTuningError, RetrievalTuning
from backend.rag.domain.search_index import SearchMode
from backend.rag.evaluation.search_eval.dataset import FIXTURE_AS_OF
from backend.rag.evaluation.search_eval.pipeline import (
    RESULT_LIMIT,
    PipelineSystem,
    SharedBase,
)
from backend.rag.evaluation.search_eval.systems import (
    IndexSystem,
    PostgresLexicalSystem,
    SearchSystem,
)
from backend.rag.infra.elasticsearch.bm25 import TEXT_FIELDS
from backend.rag.infra.elasticsearch.client import ElasticsearchClient
from backend.rag.infra.elasticsearch.indexes import ElasticsearchSearchIndex
from backend.rag.infra.elasticsearch.mappings import ANALYZER, MAPPING_VERSION
from backend.rag.infra.embeddings.bge_m3 import BgeM3Embeddings
from backend.rag.infra.embeddings.fake_bge_m3 import FakeBgeM3Embeddings
from backend.rag.infra.rerankers.cross_encoder import CrossEncoderReranker
from backend.rag.infra.rerankers.fake_overlap import FakeOverlapReranker
from backend.rag.infra.search.memory_index import InMemorySearchIndex

if TYPE_CHECKING:
    from backend.rag.application.ports.reranker import RerankerPort
    from backend.rag.application.ports.search_index import SearchIndexPort

MODES: Final = {
    "bm25": SearchMode.LEXICAL_ONLY,
    "semantic": SearchMode.SEMANTIC_ONLY,
    "hybrid": SearchMode.HYBRID,
    "hybrid-rerank": SearchMode.HYBRID,
}
ES_TIMEOUT_SECONDS: Final = 60.0
CHUNKERS: Final[dict[str, ProjectionChunker]] = {
    DEFAULT_PARAGRAPH_POLICY.version: DEFAULT_PARAGRAPH_POLICY,
    DEFAULT_POLICY.version: DEFAULT_POLICY,
}
ES_ANALYZER: Final = (
    f"{MAPPING_VERSION}/{ANALYZER}: standard tokenizer, lowercase, "
    f"asciifolding(preserve_original); multi_match most_fields {','.join(TEXT_FIELDS)}"
)


class SearchEvalConfigError(ValueError):
    """Unknown configuration name or a missing prerequisite."""


@dataclass(frozen=True, slots=True)
class SystemOptions:
    """Benchmark knobs (no secrets)."""

    configs: tuple[str, ...]
    elasticsearch_url: str | None = None
    embeddings: str = "fake-bge-m3"
    bge_m3_revision: str | None = None
    rerank_revision: str | None = None
    rerank: str = "none"
    device: str = "cpu"
    chunker: str = DEFAULT_PARAGRAPH_POLICY.version


def _embedder(options: SystemOptions) -> BatchEmbeddingPort:
    if options.embeddings == "bge-m3":
        if not options.bge_m3_revision:
            msg = "--bge-m3-revision is required for bge-m3"
            raise SearchEvalConfigError(msg)
        return BgeM3Embeddings(revision=options.bge_m3_revision, device=options.device)
    if options.embeddings != "fake-bge-m3":
        msg = "embeddings must be fake-bge-m3 or bge-m3"
        raise SearchEvalConfigError(msg)
    return FakeBgeM3Embeddings()


def _reranker(options: SystemOptions) -> "RerankerPort | None":
    """``none`` | ``fake`` (mechanics) | ``cross-encoder`` (pinned revision)."""
    mode = options.rerank
    if mode == "none" and options.rerank_revision:
        mode = "cross-encoder"
    match mode:
        case "none":
            return None
        case "fake":
            return FakeOverlapReranker()
        case "cross-encoder":
            if not options.rerank_revision:
                msg = "--rerank-revision is required for the cross-encoder"
                raise SearchEvalConfigError(msg)
            return CrossEncoderReranker(
                revision=options.rerank_revision, device=options.device
            )
        case _:
            msg = "rerank must be none, fake or cross-encoder"
            raise SearchEvalConfigError(msg)


def _chunker(name: str) -> ProjectionChunker:
    policy = CHUNKERS.get(name)
    if policy is None:
        msg = f"unknown chunker {name}; known: {sorted(CHUNKERS)}"
        raise SearchEvalConfigError(msg)
    return policy


MAX_RESULT_LIMIT: Final = 20  # BriefingRequest.result_limit upper bound


def _profile(name: str) -> tuple[RetrievalTuning, int]:
    """``profile`` or ``profile+rN`` (result_limit N, API range 1..20)."""
    profile, _, limit = name.partition("+r")
    result_limit = RESULT_LIMIT
    if limit:
        if not limit.isdigit() or not 1 <= int(limit) <= MAX_RESULT_LIMIT:
            msg = f"result limit must be 1..{MAX_RESULT_LIMIT}: {name}"
            raise SearchEvalConfigError(msg)
        result_limit = int(limit)
    try:
        return bundled_tuning(profile), result_limit
    except InvalidTuningError as error:
        raise SearchEvalConfigError(str(error)) from None


def _postgres_config(
    full_name: str,
    suffix: str,
    profile: tuple[RetrievalTuning | None, int],
    postgres: Callable[[], SearchSystem] | None,
) -> SearchSystem:
    """``postgres-lexical`` (ranking only) or ``postgres-lexical#profile``."""
    tuning, result_limit = profile
    if _chunker(suffix or DEFAULT_POLICY.version) is not DEFAULT_POLICY:
        msg = f"postgres-lexical only exists with {DEFAULT_POLICY.version}"
        raise SearchEvalConfigError(msg)
    if postgres is None:
        msg = "postgres-lexical needs --database-url"
        raise SearchEvalConfigError(msg)
    lexical = postgres()
    if tuning is None:
        return lexical
    if not isinstance(lexical, PostgresLexicalSystem):
        msg = "postgres-lexical#profile needs the PostgreSQL system"
        raise SearchEvalConfigError(msg)
    store = lexical.store

    def truth(_articles: Sequence[ArticleDocument]) -> RetrievalStore:
        return store

    return PipelineSystem(
        label=full_name,
        base=lexical,
        tuning=tuning,
        as_of=lexical.as_of,
        truth=truth,
        result_limit=result_limit,
    )


def build_systems(  # noqa: C901 - one branch per configuration family
    options: SystemOptions,
    postgres: Callable[[], SearchSystem] | None,
    *,
    as_of: datetime = FIXTURE_AS_OF,
    truth: Callable[[Sequence[ArticleDocument]], RetrievalStore] | None = None,
) -> list[SearchSystem]:
    """Configurations in the requested order.

    ``name#profile`` runs the production ``RetrieveEvidence`` pipeline with
    that retrieval tuning (stages A/B/C); ``truth`` is the store of its final
    check (default: an in-memory copy of the corpus).
    """
    embedder = _embedder(options)
    reranker = _reranker(options)
    es_index: SearchIndexPort | None = None
    es_version: str | None = None
    if options.elasticsearch_url:
        adapter = ElasticsearchSearchIndex(
            ElasticsearchClient(
                options.elasticsearch_url, timeout_seconds=ES_TIMEOUT_SECONDS
            )
        )
        es_version = adapter.health(None).engine_version
        es_index = adapter
    systems: list[SearchSystem] = []
    shared: dict[str, SharedBase] = {}
    for full_name in options.configs:
        name, _, profile = full_name.partition("#")
        tuning, result_limit = _profile(profile) if profile else (None, RESULT_LIMIT)
        base, _, suffix = name.partition("@")
        if base == "postgres-lexical":
            systems.append(
                _postgres_config(full_name, suffix, (tuning, result_limit), postgres)
            )
            continue
        chunker = _chunker(suffix or options.chunker)
        engine, _, mode_name = base.partition("-")
        if mode_name not in MODES or engine not in {"memory", "es"}:
            msg = f"unknown configuration {name}"
            raise SearchEvalConfigError(msg)
        index: SearchIndexPort
        if engine == "es":
            if es_index is None:
                msg = f"{name} needs --elasticsearch-url"
                raise SearchEvalConfigError(msg)
            index = es_index
        else:
            index = InMemorySearchIndex()
        if mode_name == "hybrid-rerank" and reranker is None:
            msg = f"{name} needs --rerank fake|cross-encoder"
            raise SearchEvalConfigError(msg)
        if tuning is not None and mode_name == "hybrid-rerank":
            msg = f"{full_name}: the pipeline runs without the reranker (off)"
            raise SearchEvalConfigError(msg)
        system = IndexSystem(
            label=name,
            mode=MODES[mode_name],
            index=index,
            embedder=None if mode_name == "bm25" else embedder,
            reranker=reranker if mode_name == "hybrid-rerank" else None,
            engine_version=es_version if engine == "es" else "memory",
            chunker=chunker,
            analyzer=ES_ANALYZER if engine == "es" else "memory-bm25 tokenizer",
        )
        if tuning is None:
            systems.append(system)
            continue
        shared_base = shared.get(name)
        if shared_base is None:
            shared_base = shared[name] = SharedBase(system)
        shared_base.users += 1
        systems.append(
            PipelineSystem(
                label=full_name,
                base=shared_base,
                tuning=tuning,
                as_of=as_of,
                truth=truth,
                result_limit=result_limit,
            )
        )
    return systems
