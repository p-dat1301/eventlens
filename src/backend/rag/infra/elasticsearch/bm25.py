"""BM25 query and the shared non-scoring filter clause."""

from backend.rag.domain.retrieval_tuning import FieldBoosts
from backend.rag.domain.search_index import SearchFilters

TEXT_FIELDS = ("title^3", "sapo^1.5", "text")


def filter_clauses(filters: SearchFilters) -> list[dict[str, object]]:
    """Filters applied identically to BM25 and kNN (pre-filter)."""
    clauses: list[dict[str, object]] = [
        {"term": {"doc_kind": "chunk"}},
        {"term": {"is_current": True}},
        {"term": {"tombstoned": False}},
        {"term": {"access_scopes": filters.scope_id}},
    ]
    if filters.source_domains:
        clauses.append({"terms": {"source_domain": list(filters.source_domains)}})
    window: dict[str, str] = {}
    if filters.published_after is not None:
        window["gte"] = filters.published_after.isoformat()
    if filters.published_before is not None:
        window["lt"] = filters.published_before.isoformat()
    if window:
        clauses.append({"range": {"published_at": window}})
    if filters.topics:
        clauses.append({"terms": {"topics": list(filters.topics)}})
    if filters.entity_ids:
        clauses.append({"terms": {"entity_ids": list(filters.entity_ids)}})
    if filters.allowed_article_ids is not None:
        clauses.append(
            {"terms": {"article_id": [str(a) for a in filters.allowed_article_ids]}}
        )
    return clauses


def text_fields(boosts: FieldBoosts | None) -> list[str]:
    """Field list with boosts; ``None`` = the frozen default."""
    if boosts is None:
        return list(TEXT_FIELDS)
    return [
        f"title^{boosts.title:g}",
        f"sapo^{boosts.sapo:g}",
        "text" if boosts.text == 1 else f"text^{boosts.text:g}",
    ]


def bm25_query(
    query: str, filters: SearchFilters, depth: int, boosts: FieldBoosts | None = None
) -> dict[str, object]:
    """``multi_match most_fields`` over title^3, sapo^1.5, text (or ``boosts``)."""
    return {
        "size": depth,
        "track_total_hits": False,
        "query": {
            "bool": {
                "must": {
                    "multi_match": {
                        "query": query,
                        "fields": text_fields(boosts),
                        "type": "most_fields",
                    }
                },
                "filter": filter_clauses(filters),
            }
        },
        "sort": ["_score", {"chunk_id": "asc"}],
    }
