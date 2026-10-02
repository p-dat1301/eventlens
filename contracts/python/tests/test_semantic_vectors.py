from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from eventlens_contracts import (
    ArticleBatchManifest,
    ArticleEnrichment,
    Event,
)
from eventlens_contracts.semantic_vectors import SemanticVector, load_semantic_vectors

REPOSITORY_ROOT = Path(__file__).parents[3]


def _apply_vector(vector: SemanticVector) -> str:
    payload = (REPOSITORY_ROOT / vector.fixture).read_text(encoding="utf-8")
    for replacement in vector.replacements:
        payload = payload.replace(replacement.old, replacement.new)
    return payload


def _validate_vector(vector: SemanticVector) -> None:
    payload = _apply_vector(vector)
    models: dict[str, type[BaseModel]] = {
        "article_batch_manifest.v1": ArticleBatchManifest,
        "event.v1": Event,
        "article_enrichment.v1": ArticleEnrichment,
    }
    _ = models[vector.contract].model_validate_json(payload)


def semantic_vectors() -> tuple[SemanticVector, ...]:
    return load_semantic_vectors(REPOSITORY_ROOT)


def test_semantic_vectors_cover_publish_cross_field_rules() -> None:
    # Given / When
    names = {
        vector.name
        for vector in semantic_vectors()
        if vector.contract
        in ("article_batch_manifest.v1", "event.v1", "article_enrichment.v1")
    }

    # Then
    assert names == {
        "valid_manifest_quality_total",
        "invalid_manifest_quality_total",
        "valid_event_detected",
        "valid_event_unconfirmed",
        "valid_event_confirmed",
        "valid_event_ongoing",
        "valid_event_resolved",
        "valid_event_cancelled",
        "valid_event_merged",
        "invalid_event_entity_fk",
        "invalid_event_duplicate_entity_role",
        "invalid_event_non_utc_timestamp",
        "invalid_event_whitespace_title",
        "valid_article_enrichment_ready",
        "invalid_enrichment_duplicate_topic",
        "invalid_enrichment_pending_output",
        "invalid_enrichment_ready_empty_output",
        "invalid_enrichment_stale_empty_output",
    }


@pytest.mark.parametrize(
    "vector",
    tuple(
        vector
        for vector in semantic_vectors()
        if vector.contract
        in ("article_batch_manifest.v1", "event.v1", "article_enrichment.v1")
    ),
)
def test_semantic_vector_matches_expected_result(vector: SemanticVector) -> None:
    # Given / When / Then
    if vector.valid:
        _validate_vector(vector)
        return
    with pytest.raises(ValidationError) as captured:
        _validate_vector(vector)
    assert captured.value.errors()[0]["type"] == vector.error_type
