from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eventlens_contracts.arrow_schema import (
    ARROW_SCHEMA_RENDERERS,
    arrow_schema_from_json,
    article_arrow_schema,
    article_media_arrow_schema,
    article_rejected_arrow_schema,
)

CONTRACTS_DIRECTORY = Path(__file__).parents[3] / "contracts"


def test_generated_arrow_snapshots_are_not_checked_in() -> None:
    # Given
    contracts_directory = CONTRACTS_DIRECTORY

    # When
    snapshots = sorted(
        path.name for path in contracts_directory.glob("*.arrow.schema.json")
    )

    # Then
    assert snapshots == []


class ParquetApi(Protocol):
    def write_table(self, table: pa.Table, where: Path) -> None: ...

    def read_schema(self, where: Path) -> pa.Schema: ...


PARQUET: ParquetApi = pq


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (
            article_arrow_schema,
            pa.schema(
                [
                    pa.field("schema_version", pa.string(), nullable=False),
                    pa.field("article_id", pa.string(), nullable=False),
                    pa.field("source_id", pa.string(), nullable=False),
                    pa.field(
                        "source",
                        pa.struct(
                            [
                                pa.field("name", pa.string(), nullable=False),
                                pa.field("domain", pa.string(), nullable=False),
                                pa.field("authority_tier", pa.string(), nullable=False),
                            ]
                        ),
                        nullable=False,
                    ),
                    pa.field("canonical_url", pa.string(), nullable=False),
                    pa.field("title", pa.string(), nullable=False),
                    pa.field("content", pa.string(), nullable=False),
                    pa.field("summary_source", pa.string()),
                    pa.field("author", pa.string()),
                    pa.field("section", pa.string()),
                    pa.field("tags", pa.list_(pa.string()), nullable=False),
                    pa.field("published_at", pa.timestamp("us", tz="UTC")),
                    pa.field("updated_at_source", pa.timestamp("us", tz="UTC")),
                    pa.field("lead_image_url", pa.string()),
                    pa.field("lead_image_storage_uri", pa.string()),
                    pa.field("media_count", pa.int32(), nullable=False),
                    pa.field("language", pa.string(), nullable=False),
                    pa.field("word_count", pa.int32(), nullable=False),
                    pa.field("content_hash", pa.string(), nullable=False),
                    pa.field(
                        "duplicate",
                        pa.struct(
                            [
                                pa.field("is_duplicate", pa.bool_(), nullable=False),
                                pa.field("canonical_article_id", pa.string()),
                                pa.field("duplicate_type", pa.string()),
                                pa.field("score", pa.float64()),
                                pa.field("method", pa.string()),
                            ]
                        ),
                        nullable=False,
                    ),
                    pa.field(
                        "raw",
                        pa.struct(
                            [
                                pa.field("article_raw_id", pa.string(), nullable=False),
                                pa.field("storage_uri", pa.string(), nullable=False),
                                pa.field("body_hash", pa.string(), nullable=False),
                                pa.field("bytes", pa.int64(), nullable=False),
                                pa.field("http_status", pa.int32(), nullable=False),
                                pa.field(
                                    "fetched_at",
                                    pa.timestamp("us", tz="UTC"),
                                    nullable=False,
                                ),
                                pa.field("crawl_run_id", pa.string(), nullable=False),
                            ]
                        ),
                        nullable=False,
                    ),
                    pa.field(
                        "processing",
                        pa.struct(
                            [
                                pa.field("parse_version", pa.string(), nullable=False),
                                pa.field("quality_status", pa.string(), nullable=False),
                                pa.field(
                                    "quality_flags",
                                    pa.list_(pa.string()),
                                    nullable=False,
                                ),
                                pa.field(
                                    "extraction_methods",
                                    pa.map_(pa.string(), pa.string()),
                                    nullable=False,
                                ),
                                pa.field(
                                    "extracted_at",
                                    pa.timestamp("us", tz="UTC"),
                                    nullable=False,
                                ),
                            ]
                        ),
                        nullable=False,
                    ),
                ],
                metadata={b"schema_version": b"article.v1"},
            ),
        ),
        (
            article_media_arrow_schema,
            pa.schema(
                [
                    pa.field("schema_version", pa.string(), nullable=False),
                    pa.field("media_id", pa.string(), nullable=False),
                    pa.field("article_id", pa.string(), nullable=False),
                    pa.field("media_type", pa.string(), nullable=False),
                    pa.field("position", pa.int32(), nullable=False),
                    pa.field("role", pa.string(), nullable=False),
                    pa.field("source_url", pa.string(), nullable=False),
                    pa.field("canonical_url", pa.string()),
                    pa.field("storage_uri", pa.string()),
                    pa.field("mime_type", pa.string()),
                    pa.field("width", pa.int32()),
                    pa.field("height", pa.int32()),
                    pa.field("bytes", pa.int64()),
                    pa.field("content_hash", pa.string()),
                    pa.field("caption", pa.string()),
                    pa.field("alt_text", pa.string()),
                    pa.field("credit", pa.string()),
                    pa.field("is_lead", pa.bool_(), nullable=False),
                    pa.field("download_status", pa.string(), nullable=False),
                    pa.field("quality_flags", pa.list_(pa.string()), nullable=False),
                    pa.field(
                        "collected_at", pa.timestamp("us", tz="UTC"), nullable=False
                    ),
                ],
                metadata={b"schema_version": b"article_media.v1"},
            ),
        ),
        (
            article_rejected_arrow_schema,
            pa.schema(
                [
                    pa.field("schema_version", pa.string(), nullable=False),
                    pa.field("article_raw_id", pa.string(), nullable=False),
                    pa.field("source_id", pa.string(), nullable=False),
                    pa.field("raw_storage_uri", pa.string(), nullable=False),
                    pa.field("reason_code", pa.string(), nullable=False),
                    pa.field("error_detail", pa.string()),
                    pa.field("parse_version", pa.string(), nullable=False),
                    pa.field(
                        "rejected_at", pa.timestamp("us", tz="UTC"), nullable=False
                    ),
                ],
                metadata={b"schema_version": b"article_rejected.v1"},
            ),
        ),
    ],
)
def test_arrow_contract_structure_is_frozen(
    build: Callable[[], pa.Schema], expected: pa.Schema
) -> None:
    # Given
    frozen_schema = expected

    # When
    actual = build()

    # Then
    assert actual.equals(frozen_schema, check_metadata=True)


@pytest.mark.parametrize(
    ("render", "generated_schema"),
    [
        (ARROW_SCHEMA_RENDERERS[0][1], article_arrow_schema()),
        (ARROW_SCHEMA_RENDERERS[1][1], article_media_arrow_schema()),
        (ARROW_SCHEMA_RENDERERS[2][1], article_rejected_arrow_schema()),
    ],
)
def test_arrow_descriptor_roundtrips_generated_schema(
    render: Callable[[], str],
    generated_schema: pa.Schema,
) -> None:
    # Given
    descriptor = render()

    # When
    reconstructed = arrow_schema_from_json(descriptor)

    # Then
    assert reconstructed.equals(generated_schema, check_metadata=True)


def test_arrow_renderers_cover_only_parquet_contracts() -> None:
    # Given / When
    artifact_names = {name for name, _renderer in ARROW_SCHEMA_RENDERERS}

    # Then
    assert artifact_names == {
        "article.v1.arrow.schema.json",
        "article_media.v1.arrow.schema.json",
        "article_rejected.v1.arrow.schema.json",
    }


def test_pyarrow_and_polars_roundtrip_media_schema(tmp_path: Path) -> None:
    # Given
    source_path = tmp_path / "media-pyarrow.parquet"
    polars_path = tmp_path / "media-polars.parquet"
    schema = article_media_arrow_schema()
    table = pa.Table.from_pylist([], schema=schema)

    # When
    PARQUET.write_table(table, source_path)
    frame = pl.read_parquet(source_path)
    roundtrip_table = frame.to_arrow().cast(schema.remove_metadata())
    PARQUET.write_table(roundtrip_table, polars_path)
    pyarrow_schema = PARQUET.read_schema(source_path)
    polars_schema = PARQUET.read_schema(polars_path)

    # Then
    assert pyarrow_schema.equals(schema.remove_metadata())
    assert polars_schema.equals(schema.remove_metadata())
