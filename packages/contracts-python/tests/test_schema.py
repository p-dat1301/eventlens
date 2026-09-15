from pathlib import Path

from eventlens_contracts.schema import (
    article_batch_manifest_schema_json,
    article_media_schema_json,
    article_rejected_schema_json,
    article_schema_json,
    event_schema_json,
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


def test_event_schema_artifact_matches_model() -> None:
    # Given
    artifact = (SCHEMA_DIRECTORY / "event.v1.schema.json").read_text(encoding="utf-8")

    # When
    generated = event_schema_json()

    # Then
    assert artifact == generated


def test_event_schema_exposes_storage_and_text_constraints() -> None:
    # Given / When
    rendered = event_schema_json()

    # Then
    assert rendered.count('"maximum": 2147483647') == 2
    assert rendered.count('"minLength": 1') == 2
    assert rendered.count('"pattern": "\\\\S"') == 2
    assert '"$ref": "#/$defs/JsonObject"' in rendered
    assert '"$ref": "#/$defs/JsonValue"' in rendered


def test_event_schema_renderer_is_pretty_printed() -> None:
    # Given / When
    rendered = event_schema_json()

    # Then
    assert rendered.startswith('{\n  "$defs":')
