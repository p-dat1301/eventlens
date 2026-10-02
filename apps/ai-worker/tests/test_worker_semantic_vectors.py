from pathlib import Path

import pytest
from pydantic import ValidationError

from eventlens_contracts.semantic_vectors import SemanticVector, load_semantic_vectors
from eventlens_worker.bundle import ArticleBundle

ROOT = Path(__file__).parents[3]
VECTORS = tuple(
    vector
    for vector in load_semantic_vectors(ROOT)
    if vector.contract == "article_bundle.v1"
)


def test_bundle_vectors_cover_all_rules() -> None:
    assert {vector.name for vector in VECTORS} == {
        "valid_article_bundle",
        "invalid_media_count",
        "invalid_media_fk",
        "invalid_duplicate_media_id",
        "invalid_non_contiguous_position",
    }


@pytest.mark.parametrize("vector", VECTORS)
def test_bundle_vector_matches_expected_result(vector: SemanticVector) -> None:
    payload = (ROOT / vector.fixture).read_text(encoding="utf-8")
    for replacement in vector.replacements:
        payload = payload.replace(replacement.old, replacement.new)
    if vector.valid:
        _ = ArticleBundle.model_validate_json(payload)
        return
    with pytest.raises(ValidationError) as captured:
        _ = ArticleBundle.model_validate_json(payload)
    assert captured.value.errors()[0]["type"] == vector.error_type
