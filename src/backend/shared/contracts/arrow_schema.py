"""Deterministic Arrow schemas for Parquet contracts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, ClassVar, Literal

import pyarrow as pa
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable

ArrowTypeName = Literal[
    "string",
    "bool",
    "int32",
    "int64",
    "float64",
    "timestamp[us, UTC]",
    "list<string>",
    "map<string, string>",
    "struct",
]


class ArrowFieldDescriptor(BaseModel):
    """Portable Arrow field description."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True
    )

    name: str
    type: ArrowTypeName
    nullable: bool
    fields: tuple[ArrowFieldDescriptor, ...] = ()


class ArrowSchemaDescriptor(BaseModel):
    """Portable Arrow schema artifact."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True
    )

    schema_version: Literal["article.v1", "article_media.v1", "article_rejected.v1"]
    fields: tuple[ArrowFieldDescriptor, ...]


def _field(
    name: str,
    type_name: ArrowTypeName,
    *,
    nullable: bool = False,
) -> ArrowFieldDescriptor:
    return ArrowFieldDescriptor(
        name=name,
        type=type_name,
        nullable=nullable,
    )


def _struct(
    name: str,
    fields: tuple[ArrowFieldDescriptor, ...],
) -> ArrowFieldDescriptor:
    return ArrowFieldDescriptor(name=name, type="struct", nullable=False, fields=fields)


def _data_type(field: ArrowFieldDescriptor) -> pa.DataType:
    match field.type:
        case "string":
            data_type = pa.string()
        case "bool":
            data_type = pa.bool_()
        case "int32":
            data_type = pa.int32()
        case "int64":
            data_type = pa.int64()
        case "float64":
            data_type = pa.float64()
        case "timestamp[us, UTC]":
            data_type = pa.timestamp("us", tz="UTC")
        case "list<string>":
            data_type = pa.list_(pa.string())
        case "map<string, string>":
            data_type = pa.map_(pa.string(), pa.string())
        case "struct":
            data_type = pa.struct([_arrow_field(child) for child in field.fields])
    return data_type


def _arrow_field(field: ArrowFieldDescriptor) -> pa.Field[pa.DataType]:
    return pa.field(field.name, _data_type(field), nullable=field.nullable)


def _schema(descriptor: ArrowSchemaDescriptor) -> pa.Schema:
    metadata: dict[bytes | str, bytes | str] = {
        b"schema_version": descriptor.schema_version.encode("ascii")
    }
    return pa.schema(
        [_arrow_field(field) for field in descriptor.fields], metadata=metadata
    )


ARTICLE_ARROW_DESCRIPTOR = ArrowSchemaDescriptor(
    schema_version="article.v1",
    fields=(
        _field("schema_version", "string"),
        _field("article_id", "string"),
        _field("source_id", "string"),
        _struct(
            "source",
            (
                _field("name", "string"),
                _field("domain", "string"),
                _field("authority_tier", "string"),
            ),
        ),
        _field("canonical_url", "string"),
        _field("title", "string"),
        _field("content", "string"),
        _field("summary_source", "string", nullable=True),
        _field("author", "string", nullable=True),
        _field("section", "string", nullable=True),
        _field("tags", "list<string>"),
        _field("published_at", "timestamp[us, UTC]", nullable=True),
        _field("updated_at_source", "timestamp[us, UTC]", nullable=True),
        _field("lead_image_url", "string", nullable=True),
        _field("lead_image_storage_uri", "string", nullable=True),
        _field("media_count", "int32"),
        _field("language", "string"),
        _field("word_count", "int32"),
        _field("content_hash", "string"),
        _struct(
            "duplicate",
            (
                _field("is_duplicate", "bool"),
                _field("canonical_article_id", "string", nullable=True),
                _field("duplicate_type", "string", nullable=True),
                _field("score", "float64", nullable=True),
                _field("method", "string", nullable=True),
            ),
        ),
        _struct(
            "raw",
            (
                _field("article_raw_id", "string"),
                _field("storage_uri", "string"),
                _field("body_hash", "string"),
                _field("bytes", "int64"),
                _field("http_status", "int32"),
                _field("fetched_at", "timestamp[us, UTC]"),
                _field("crawl_run_id", "string"),
            ),
        ),
        _struct(
            "processing",
            (
                _field("parse_version", "string"),
                _field("quality_status", "string"),
                _field("quality_flags", "list<string>"),
                _field("extraction_methods", "map<string, string>"),
                _field("extracted_at", "timestamp[us, UTC]"),
            ),
        ),
    ),
)

ARTICLE_MEDIA_ARROW_DESCRIPTOR = ArrowSchemaDescriptor(
    schema_version="article_media.v1",
    fields=(
        _field("schema_version", "string"),
        _field("media_id", "string"),
        _field("article_id", "string"),
        _field("media_type", "string"),
        _field("position", "int32"),
        _field("role", "string"),
        _field("source_url", "string"),
        _field("canonical_url", "string", nullable=True),
        _field("storage_uri", "string", nullable=True),
        _field("mime_type", "string", nullable=True),
        _field("width", "int32", nullable=True),
        _field("height", "int32", nullable=True),
        _field("bytes", "int64", nullable=True),
        _field("content_hash", "string", nullable=True),
        _field("caption", "string", nullable=True),
        _field("alt_text", "string", nullable=True),
        _field("credit", "string", nullable=True),
        _field("is_lead", "bool"),
        _field("download_status", "string"),
        _field("quality_flags", "list<string>"),
        _field("collected_at", "timestamp[us, UTC]"),
    ),
)

ARTICLE_REJECTED_ARROW_DESCRIPTOR = ArrowSchemaDescriptor(
    schema_version="article_rejected.v1",
    fields=(
        _field("schema_version", "string"),
        _field("article_raw_id", "string"),
        _field("source_id", "string"),
        _field("raw_storage_uri", "string"),
        _field("reason_code", "string"),
        _field("error_detail", "string", nullable=True),
        _field("parse_version", "string"),
        _field("rejected_at", "timestamp[us, UTC]"),
    ),
)


def article_arrow_schema() -> pa.Schema:
    """Build article.v1 Arrow schema."""
    return _schema(ARTICLE_ARROW_DESCRIPTOR)


def article_media_arrow_schema() -> pa.Schema:
    """Build article_media.v1 Arrow schema."""
    return _schema(ARTICLE_MEDIA_ARROW_DESCRIPTOR)


def article_rejected_arrow_schema() -> pa.Schema:
    """Build article_rejected.v1 Arrow schema."""
    return _schema(ARTICLE_REJECTED_ARROW_DESCRIPTOR)


def arrow_schema_from_json(payload: str) -> pa.Schema:
    """Reconstruct exact Arrow schema from portable descriptor."""
    return _schema(ArrowSchemaDescriptor.model_validate_json(payload))


def _descriptor_json(descriptor: ArrowSchemaDescriptor) -> str:
    return f"{json.dumps(descriptor.model_dump(mode='json'), indent=2)}\n"


ARROW_SCHEMA_RENDERERS: tuple[tuple[str, Callable[[], str]], ...] = (
    (
        "article.v1.arrow.schema.json",
        lambda: _descriptor_json(ARTICLE_ARROW_DESCRIPTOR),
    ),
    (
        "article_media.v1.arrow.schema.json",
        lambda: _descriptor_json(ARTICLE_MEDIA_ARROW_DESCRIPTOR),
    ),
    (
        "article_rejected.v1.arrow.schema.json",
        lambda: _descriptor_json(ARTICLE_REJECTED_ARROW_DESCRIPTOR),
    ),
)
