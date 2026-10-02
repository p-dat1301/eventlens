from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

import pytest
from pydantic import TypeAdapter

from eventlens_contracts.schema import (
    SCHEMA_RENDERERS,
    article_batch_manifest_schema_json,
    article_enrichment_schema_json,
    article_media_schema_json,
    article_rejected_schema_json,
    article_schema_json,
    event_schema_json,
)

SCHEMA_DIRECTORY = Path(__file__).parents[3] / "contracts"


SchemaProperty = TypedDict(
    "SchemaProperty",
    {
        "const": str,
        "type": str,
        "format": str,
        "pattern": str,
        "minLength": int,
        "minimum": int,
        "maximum": int,
        "$ref": str,
        "items": "SchemaProperty",
        "anyOf": list["SchemaProperty"],
    },
    total=False,
)


class SchemaShape(TypedDict):
    required: list[str]
    properties: dict[str, SchemaProperty]


SCHEMA_ADAPTER: TypeAdapter[SchemaShape] = TypeAdapter(SchemaShape)


def test_generated_schema_snapshots_are_not_checked_in() -> None:
    # Given
    contracts_directory = SCHEMA_DIRECTORY

    # When
    snapshots = sorted(path.name for path in contracts_directory.glob("*.schema.json"))

    # Then
    assert snapshots == []


@pytest.mark.parametrize(("schema_name", "renderer"), SCHEMA_RENDERERS)
def test_schema_renderer_produces_required_contract(
    schema_name: str, renderer: Callable[[], str]
) -> None:
    # Given
    version = schema_name.removesuffix(".schema.json")

    # When
    schema = SCHEMA_ADAPTER.validate_json(renderer())

    # Then
    assert "schema_version" in schema["required"]
    assert schema["properties"]["schema_version"].get("const") == version


def test_article_schema_requires_identity_and_nonempty_title() -> None:
    schema = SCHEMA_ADAPTER.validate_json(article_schema_json())
    assert {"article_id", "source_id", "title", "content"} <= set(schema["required"])
    assert schema["properties"]["title"].get("minLength") == 1


def test_media_schema_bounds_position() -> None:
    schema = SCHEMA_ADAPTER.validate_json(article_media_schema_json())
    assert schema["properties"]["position"].get("minimum") == 0
    assert schema["properties"]["position"].get("maximum") == 2147483647


def test_rejected_schema_requires_reason() -> None:
    schema = SCHEMA_ADAPTER.validate_json(article_rejected_schema_json())
    assert "reason_code" in schema["required"]


def test_manifest_schema_requires_quality() -> None:
    schema = SCHEMA_ADAPTER.validate_json(article_batch_manifest_schema_json())
    assert "quality" in schema["required"]


def test_enrichment_schema_requires_article_reference() -> None:
    schema = SCHEMA_ADAPTER.validate_json(article_enrichment_schema_json())
    assert "article_id" in schema["required"]


@pytest.mark.parametrize(
    ("renderer", "required", "properties"),
    [
        (
            article_schema_json,
            {
                "schema_version",
                "article_id",
                "source_id",
                "source",
                "canonical_url",
                "title",
                "content",
                "tags",
                "media_count",
                "language",
                "word_count",
                "content_hash",
                "duplicate",
                "raw",
                "processing",
            },
            {
                "article_id": {"type": "string", "format": "uuid"},
                "source": {"$ref": "#/$defs/ArticleSource"},
                "raw": {"$ref": "#/$defs/ArticleRaw"},
                "media_count": {"type": "integer", "minimum": 0, "maximum": 2147483647},
                "content_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            },
        ),
        (
            article_media_schema_json,
            {
                "schema_version",
                "media_id",
                "article_id",
                "media_type",
                "position",
                "role",
                "source_url",
                "is_lead",
                "download_status",
                "quality_flags",
                "collected_at",
            },
            {
                "position": {"type": "integer", "minimum": 0, "maximum": 2147483647},
                "source_url": {"type": "string", "format": "uri"},
                "quality_flags": {"type": "array", "items": {"type": "string"}},
            },
        ),
        (
            article_rejected_schema_json,
            {
                "schema_version",
                "article_raw_id",
                "source_id",
                "raw_storage_uri",
                "reason_code",
                "parse_version",
                "rejected_at",
            },
            {
                "raw_storage_uri": {"type": "string", "pattern": r"^s3://[^/\s]+/.+$"},
                "reason_code": {"type": "string", "minLength": 1, "pattern": r"\S"},
            },
        ),
        (
            article_batch_manifest_schema_json,
            {
                "schema_version",
                "batch_id",
                "source",
                "partition",
                "files",
                "quality",
                "parse_versions",
                "created_at",
            },
            {
                "files": {"type": "array", "items": {"$ref": "#/$defs/ManifestFile"}},
                "quality": {"$ref": "#/$defs/ManifestQuality"},
            },
        ),
        (
            event_schema_json,
            {
                "id",
                "event_type",
                "canonical_title",
                "status",
                "first_seen_at",
                "last_seen_at",
                "last_changed_at",
                "source_count",
                "article_count",
                "schema_version",
                "created_at",
                "updated_at",
                "entities",
            },
            {
                "entities": {"type": "array", "items": {"$ref": "#/$defs/EventEntity"}},
                "source_count": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2147483647,
                },
                "attributes": {
                    "anyOf": [{"$ref": "#/$defs/JsonObject"}, {"type": "null"}]
                },
            },
        ),
        (
            article_enrichment_schema_json,
            {
                "schema_version",
                "article_id",
                "content_hash",
                "enrichment_version",
                "model_version",
                "prompt_version",
                "status",
                "topics",
                "content_type",
                "event_relevance",
                "entities",
                "locations",
                "suggested_tags",
                "evidence_spans",
                "field_confidence",
                "enriched_at",
                "attempt_count",
                "failure_code",
            },
            {
                "evidence_spans": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/EvidenceSpan"},
                },
                "attempt_count": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2147483647,
                },
            },
        ),
    ],
)
def test_shared_schema_keeps_structural_contract(
    renderer: Callable[[], str],
    required: set[str],
    properties: dict[str, SchemaProperty],
) -> None:
    # Given
    expected_properties = properties

    # When
    schema = SCHEMA_ADAPTER.validate_json(renderer())

    # Then
    assert required == set(schema["required"])
    for name, expected in expected_properties.items():
        assert expected.items() <= schema["properties"][name].items()


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
