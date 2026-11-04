"""Elasticsearch request builders, endpoint policy, model adapters, settings.

No network: builders are pure, the client is only constructed, and model
adapters get injected fake loaders (nothing is downloaded).
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest

from backend.rag.application.ports.reranker import RerankCandidate, RerankRequest
from backend.rag.application.ports.search_index import SearchIndexUnavailableError
from backend.rag.composition import (
    RerankMode,
    SearchBackend,
    SearchConfigurationError,
    SearchEmbeddings,
    SearchSettings,
)
from backend.rag.domain.retrieval_tuning import BASELINE_TUNING, FieldBoosts
from backend.rag.domain.search_embedding import BGE_M3_IDENTITY
from backend.rag.domain.search_index import SearchFilters
from backend.rag.infra.elasticsearch.bm25 import bm25_query, filter_clauses
from backend.rag.infra.elasticsearch.client import (
    ElasticsearchClient,
    InsecureEndpointError,
)
from backend.rag.infra.elasticsearch.errors import http_error
from backend.rag.infra.elasticsearch.mappings import (
    MAPPING_VERSION,
    index_body,
    mapping_digest,
)
from backend.rag.infra.elasticsearch.vector import knn_query
from backend.rag.infra.embeddings.bge_m3 import BgeM3Embeddings
from backend.rag.infra.embeddings.fake_bge_m3 import FakeBgeM3Embeddings, fold
from backend.rag.infra.rerankers.cross_encoder import CrossEncoderReranker
from backend.rag.infra.retrieval.identity_reranker import IdentityReranker


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def _items(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast("list[object]", value)


FILTERS = SearchFilters(
    scope_id="fixture-internal",
    source_domains=("vnexpress.net",),
    published_after=datetime(2026, 1, 1, tzinfo=UTC),
    published_before=datetime(2026, 2, 1, tzinfo=UTC),
    topics=("transport",),
    entity_ids=("e1",),
    allowed_article_ids=(uuid4(),),
)


def test_bm25_and_knn_share_the_exact_same_filters() -> None:
    lexical = bm25_query("cảng Hải Phòng", FILTERS, 50)
    semantic = knn_query((0.1,) * 1024, FILTERS, 50)
    lexical_filters = _items(_mapping(_mapping(lexical["query"])["bool"])["filter"])
    semantic_filters = _items(
        _mapping(_mapping(_mapping(semantic["knn"])["filter"])["bool"])["filter"]
    )
    assert lexical_filters == semantic_filters == filter_clauses(FILTERS)
    assert {"term": {"tombstoned": False}} in lexical_filters
    assert {"term": {"is_current": True}} in lexical_filters
    assert {"term": {"access_scopes": "fixture-internal"}} in lexical_filters


def test_bm25_boosts_title_above_body() -> None:
    fields = _mapping(
        _mapping(
            _mapping(_mapping(bm25_query("x", FILTERS, 10)["query"])["bool"])["must"]
        )["multi_match"]
    )["fields"]
    assert fields == ["title^3", "sapo^1.5", "text"]


def test_publication_window_matches_postgres_semantics() -> None:
    (window,) = [c for c in filter_clauses(FILTERS) if "range" in c]
    bounds = _mapping(_mapping(window["range"])["published_at"])
    assert set(bounds) == {"gte", "lt"}  # inclusive start, exclusive end


def test_mapping_is_strict_versioned_and_has_vietnamese_folding() -> None:
    body = index_body(1024)
    mappings = _mapping(body["mappings"])
    assert mappings["dynamic"] == "strict"
    assert mappings["_meta"] == {"mapping_version": MAPPING_VERSION}
    embedding = _mapping(_mapping(mappings["properties"])["embedding"])
    assert embedding["dims"] == 1024
    assert embedding["similarity"] == "cosine"
    analyzer = _mapping(
        _mapping(_mapping(_mapping(body["settings"])["analysis"])["analyzer"])[
            "vi_text"
        ]
    )
    assert analyzer["filter"] == ["lowercase", "vi_fold"]
    assert "embedding" not in _mapping(
        _mapping(index_body(None)["mappings"])["properties"]
    )
    assert mapping_digest(1024) != mapping_digest(None)


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("http://search.internal:9200", "k"),
        ("https://search.internal:9200", None),
        ("https://user:pw@search.internal", "k"),
        ("ftp://127.0.0.1", None),
    ],
)
def test_non_loopback_endpoints_need_https_and_a_key(url: str, key: str | None) -> None:
    with pytest.raises(InsecureEndpointError):
        _ = ElasticsearchClient(url, key)


def test_loopback_http_and_https_with_key_are_accepted() -> None:
    _ = ElasticsearchClient("http://127.0.0.1:9200")
    _ = ElasticsearchClient("https://search.internal", "api-key")


def test_unreachable_engine_is_a_typed_unavailable_error() -> None:
    client = ElasticsearchClient("http://127.0.0.1:9", timeout_seconds=0.5)
    with pytest.raises(SearchIndexUnavailableError):
        _ = client.request("GET", "/")


def test_http_status_mapping() -> None:
    assert isinstance(http_error(503, None), SearchIndexUnavailableError)
    assert not isinstance(
        http_error(400, "parsing_exception"), SearchIndexUnavailableError
    )


def test_fake_bge_m3_is_1024_deterministic_and_accent_aware() -> None:
    fake = FakeBgeM3Embeddings()
    first = fake.embed_query("Hà Nội mưa lớn")
    assert len(first) == 1024
    assert first == FakeBgeM3Embeddings().embed_query("Hà Nội mưa lớn")
    near = fake.embed_query("ha noi mua lon")
    far = fake.embed_query("giá vàng tăng mạnh")
    dot = sum(a * b for a, b in zip(first, near, strict=True))
    assert dot > sum(a * b for a, b in zip(first, far, strict=True))
    assert fold("Đường Lê Lợi") == "duong le loi"


class _FakeEncoder:
    max_seq_length: int = 0

    def __init__(self, dims: int) -> None:
        self.dims: int = dims
        self.calls: int = 0

    def encode(
        self,
        sentences: list[str],
        *,
        batch_size: int,
        normalize_embeddings: bool,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> Sequence[Sequence[float]]:
        del batch_size, normalize_embeddings, convert_to_numpy, show_progress_bar
        self.calls += 1
        return [[float(i + 1) for i in range(self.dims)] for _ in sentences]


def test_bge_m3_adapter_checks_dimensions_and_never_downloads() -> None:
    seen: list[tuple[str, str, str]] = []
    encoder = _FakeEncoder(1024)

    def loader(model: str, revision: str, device: str) -> _FakeEncoder:
        seen.append((model, revision, device))
        return encoder

    adapter = BgeM3Embeddings(revision="abc123", loader=loader)
    vectors = adapter.embed_documents(["một", "hai"])
    assert len(vectors) == 2
    assert all(len(v) == 1024 for v in vectors)
    assert abs(sum(v * v for v in vectors[0]) - 1.0) < 1e-9
    assert seen == [("BAAI/bge-m3", "abc123", "cpu")]
    assert adapter.identity.identity == BGE_M3_IDENTITY
    assert adapter.identity.revision == "abc123"

    def wrong_loader(_model: str, _revision: str, _device: str) -> _FakeEncoder:
        return _FakeEncoder(768)

    wrong = BgeM3Embeddings(revision="abc123", loader=wrong_loader)
    with pytest.raises(ValueError, match="dimension"):
        _ = wrong.embed_query("x")


def test_bge_m3_requires_a_pinned_revision() -> None:
    with pytest.raises(ValueError, match="revision"):
        _ = BgeM3Embeddings(revision=" ")


class _FakeCrossEncoder:
    def predict(
        self,
        sentences: list[tuple[str, str]],
        *,
        batch_size: int,
        show_progress_bar: bool,
    ) -> Sequence[float]:
        del batch_size, show_progress_bar
        return [float(len(text)) for _, text in sentences]


def test_cross_encoder_scores_exactly_the_requested_candidates() -> None:
    def loader(
        _model: str, _revision: str, _device: str, _max_length: int
    ) -> _FakeCrossEncoder:
        return _FakeCrossEncoder()

    reranker = CrossEncoderReranker(
        revision="r1",
        loader=loader,
    )
    ids = [uuid4(), uuid4()]
    output = reranker.rerank(
        RerankRequest(
            "q",
            (RerankCandidate(ids[0], "ngắn"), RerankCandidate(ids[1], "dài hơn")),
            1.0,
        )
    )
    assert [candidate for candidate, _ in output.scores] == ids
    assert reranker.identity.reranker_id == "BAAI/bge-reranker-v2-m3"


def test_settings_default_to_postgres_identity_reranker_and_no_hops() -> None:
    settings = SearchSettings.from_environment({})
    assert settings.backend is SearchBackend.POSTGRES
    assert settings.embeddings is SearchEmbeddings.NONE
    assert settings.rerank is RerankMode.OFF
    assert not settings.multi_hop
    assert settings.fallback_to_postgres
    assert isinstance(settings.reranker(), IdentityReranker)
    assert settings.embedder() is None
    assert settings.retrieval_tuning() == BASELINE_TUNING


def test_retrieval_tuning_profile_is_explicit_and_reversible() -> None:
    chosen = SearchSettings.from_environment({"EVENTLENS_RAG_RETRIEVAL_TUNING": "div3"})
    back = SearchSettings.from_environment(
        {"EVENTLENS_RAG_RETRIEVAL_TUNING": "baseline"}
    )

    assert chosen.retrieval_tuning().tuning_id == "div3"
    assert back.retrieval_tuning() == BASELINE_TUNING


def test_bm25_boosts_are_configurable_and_default_unchanged() -> None:
    default = bm25_query("x", FILTERS, 10)
    tuned = bm25_query("x", FILTERS, 10, FieldBoosts(title=5, sapo=2, text=1))

    assert "title^3" in str(default)
    assert "sapo^1.5" in str(default)
    assert "title^5" in str(tuned)
    assert "sapo^2" in str(tuned)
    assert bm25_query("x", FILTERS, 10, FieldBoosts()) == default


@pytest.mark.parametrize(
    "environ",
    [
        {"EVENTLENS_RAG_SEARCH_BACKEND": "elasticsearch"},
        {"EVENTLENS_RAG_SEARCH_EMBEDDINGS": "bge-m3"},
        {"EVENTLENS_RAG_RERANK": "cross-encoder"},
        {"EVENTLENS_RAG_RERANK": "always"},
        {"EVENTLENS_RAG_MULTI_HOP": "yes"},
        {"EVENTLENS_RAG_SEARCH_FALLBACK": "maybe"},
        {"EVENTLENS_RAG_RETRIEVAL_TUNING": "unknown-profile"},
    ],
)
def test_incomplete_or_unknown_settings_fail_fast(environ: dict[str, str]) -> None:
    with pytest.raises(SearchConfigurationError):
        _ = SearchSettings.from_environment(environ)


def test_live_models_need_explicit_activation() -> None:
    settings = SearchSettings.from_environment(
        {
            "EVENTLENS_RAG_SEARCH_EMBEDDINGS": "bge-m3",
            "EVENTLENS_RAG_BGE_M3_REVISION": "5617a9f61b028005a4858fdac845db406aefb181",
        }
    )
    embedder = settings.embedder()
    assert isinstance(embedder, BgeM3Embeddings)
    assert embedder.identity.identity == "bge-m3-1024.v1"
