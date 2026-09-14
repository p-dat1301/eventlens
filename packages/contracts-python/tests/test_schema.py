from pathlib import Path

from eventlens_contracts.schema import (
    article_batch_manifest_schema_json,
    article_media_schema_json,
    article_rejected_schema_json,
    article_schema_json,
)

SCHEMA_DIRECTORY = Path(__file__).parents[3] / "contracts"


def test_article_schema_artifact_matches_model() -> None:
    # Given
    artifact = (SCHEMA_DIRECTORY / "article.v1.schema.json").read_text(encoding="utf-8")

    # When
    generated = article_schema_json()

    # Then
    assert artifact == generated


def test_article_media_schema_artifact_matches_model() -> None:
    # Given
    artifact = (SCHEMA_DIRECTORY / "article_media.v1.schema.json").read_text(
        encoding="utf-8"
    )

    # When
    generated = article_media_schema_json()

    # Then
    assert artifact == generated


def test_article_rejected_schema_artifact_matches_model() -> None:
    # Given
    artifact = (SCHEMA_DIRECTORY / "article_rejected.v1.schema.json").read_text(
        encoding="utf-8"
    )

    # When
    generated = article_rejected_schema_json()

    # Then
    assert artifact == generated


def test_article_batch_manifest_schema_artifact_matches_model() -> None:
    # Given
    artifact = (SCHEMA_DIRECTORY / "article_batch_manifest.v1.schema.json").read_text(
        encoding="utf-8"
    )

    # When
    generated = article_batch_manifest_schema_json()

    # Then
    assert artifact == generated
