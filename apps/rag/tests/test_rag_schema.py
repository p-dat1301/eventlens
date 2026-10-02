from collections.abc import Callable
from typing import TypedDict

import pytest
from pydantic import TypeAdapter

from eventlens_rag.schema import (
    article_document_schema_json,
    briefing_answer_schema_json,
    briefing_query_schema_json,
)

SchemaProperty = TypedDict(
    "SchemaProperty",
    {
        "const": str,
        "type": str,
        "pattern": str,
        "minimum": int,
        "maximum": int,
        "minLength": int,
        "$ref": str,
        "items": "SchemaProperty",
    },
    total=False,
)


class SchemaShape(TypedDict):
    required: list[str]
    properties: dict[str, SchemaProperty]


SCHEMA_ADAPTER: TypeAdapter[SchemaShape] = TypeAdapter(SchemaShape)


@pytest.mark.parametrize(
    ("schema_name", "renderer"),
    [
        ("article_document.v1.schema.json", article_document_schema_json),
        ("briefing_query.v1.schema.json", briefing_query_schema_json),
        ("briefing_answer.v1.schema.json", briefing_answer_schema_json),
    ],
)
def test_rag_schema_renderer_requires_version(
    schema_name: str, renderer: Callable[[], str]
) -> None:
    # Given
    version = schema_name.removesuffix(".schema.json")

    # When
    schema = SCHEMA_ADAPTER.validate_json(renderer())

    # Then
    assert "schema_version" in schema["required"]
    assert schema["properties"]["schema_version"].get("const") == version


@pytest.mark.parametrize(
    ("renderer", "required", "properties"),
    [
        (
            article_document_schema_json,
            {
                "schema_version",
                "article_id",
                "source_id",
                "title",
                "raw_content",
                "normalization_version",
                "normalized_content",
                "canonical_url",
                "source_name",
                "source_domain",
                "source_published_at",
                "ingested_at",
                "processed_at",
                "content_hash",
                "content_fingerprint",
                "visibility",
                "evidence_spans",
            },
            {
                "normalized_content": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": r"\S",
                },
                "content_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                "visibility": {"type": "string", "const": "internal"},
                "evidence_spans": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/EvidenceSpan"},
                },
            },
        ),
        (
            briefing_query_schema_json,
            {
                "schema_version",
                "query",
                "publication_time_basis",
                "result_limit",
                "context_budget",
            },
            {
                "query": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "result_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "context_budget": {"type": "integer", "minimum": 1, "maximum": 12000},
            },
        ),
        (
            briefing_answer_schema_json,
            {
                "schema_version",
                "claims",
                "citations",
                "uncertainties",
                "display_blocks",
                "answer",
                "source_documents",
                "retrieved_at",
                "model_version",
                "index_version",
            },
            {
                "claims": {"type": "array", "items": {"$ref": "#/$defs/Claim"}},
                "citations": {"type": "array", "items": {"$ref": "#/$defs/Citation"}},
                "source_documents": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/ArticleDocument"},
                },
                "answer": {"type": "string", "minLength": 1, "pattern": r"\S"},
            },
        ),
    ],
)
def test_rag_schema_keeps_structural_contract(
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
