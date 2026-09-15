from pathlib import Path
from typing import ClassVar, Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from eventlens_contracts import ArticleBatchManifest, ArticleBundle, Event

CONTRACTS_DIRECTORY = Path(__file__).parents[3] / "contracts"
REPOSITORY_ROOT = Path(__file__).parents[3]
type ContractName = Literal[
    "article_bundle.v1", "article_batch_manifest.v1", "event.v1"
]


class Replacement(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True
    )

    old: str
    new: str


class SemanticVector(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True
    )

    name: str
    contract: ContractName
    fixture: str
    replacements: tuple[Replacement, ...]
    valid: bool
    error_type: str | None


class SemanticVectors(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True
    )

    schema_version: Literal["eventlens_semantic_vectors.v1"]
    vectors: tuple[SemanticVector, ...]


def _apply_vector(vector: SemanticVector) -> str:
    payload = (REPOSITORY_ROOT / vector.fixture).read_text(encoding="utf-8")
    for replacement in vector.replacements:
        payload = payload.replace(replacement.old, replacement.new)
    return payload


def _validate_vector(vector: SemanticVector) -> None:
    payload = _apply_vector(vector)
    models: dict[ContractName, type[BaseModel]] = {
        "article_bundle.v1": ArticleBundle,
        "article_batch_manifest.v1": ArticleBatchManifest,
        "event.v1": Event,
    }
    _ = models[vector.contract].model_validate_json(payload)


def semantic_vectors() -> SemanticVectors:
    artifact = (CONTRACTS_DIRECTORY / "semantic_vectors.v1.json").read_text(
        encoding="utf-8"
    )
    return SemanticVectors.model_validate_json(artifact)


def test_semantic_vectors_cover_publish_cross_field_rules() -> None:
    # Given / When
    names = {vector.name for vector in semantic_vectors().vectors}

    # Then
    assert names == {
        "valid_article_bundle",
        "invalid_media_count",
        "invalid_media_fk",
        "invalid_duplicate_media_id",
        "invalid_non_contiguous_position",
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
    }


@pytest.mark.parametrize("vector", semantic_vectors().vectors)
def test_semantic_vector_matches_expected_result(vector: SemanticVector) -> None:
    # Given / When / Then
    if vector.valid:
        _validate_vector(vector)
        return
    with pytest.raises(ValidationError) as captured:
        _validate_vector(vector)
    assert captured.value.errors()[0]["type"] == vector.error_type
