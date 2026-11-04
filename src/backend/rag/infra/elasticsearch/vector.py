"""kNN query over the cosine ``dense_vector`` with the shared pre-filter."""

from backend.rag.domain.search_index import SearchFilters
from backend.rag.infra.elasticsearch.bm25 import filter_clauses

NUM_CANDIDATES_FACTOR = 4


def knn_query(
    vector: tuple[float, ...], filters: SearchFilters, depth: int
) -> dict[str, object]:
    """Approximate kNN (HNSW); ``num_candidates`` = 4 x depth."""
    return {
        "size": depth,
        "knn": {
            "field": "embedding",
            "query_vector": list(vector),
            "k": depth,
            "num_candidates": max(depth * NUM_CANDIDATES_FACTOR, 100),
            "filter": {"bool": {"filter": filter_clauses(filters)}},
        },
    }
