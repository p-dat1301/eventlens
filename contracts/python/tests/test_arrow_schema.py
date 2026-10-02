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


class ParquetApi(Protocol):
    def write_table(self, table: pa.Table, where: Path) -> None: ...

    def read_schema(self, where: Path) -> pa.Schema: ...


PARQUET: ParquetApi = pq


@pytest.mark.parametrize(
    ("artifact_name", "generated_schema"),
    [
        ("article.v1.arrow.schema.json", article_arrow_schema()),
        ("article_media.v1.arrow.schema.json", article_media_arrow_schema()),
        ("article_rejected.v1.arrow.schema.json", article_rejected_arrow_schema()),
    ],
)
def test_arrow_artifact_reconstructs_exact_generated_schema(
    artifact_name: str,
    generated_schema: pa.Schema,
) -> None:
    # Given
    artifact = (CONTRACTS_DIRECTORY / artifact_name).read_text(encoding="utf-8")

    # When
    reconstructed = arrow_schema_from_json(artifact)

    # Then
    assert reconstructed.equals(generated_schema, check_metadata=True)


@pytest.mark.parametrize(("artifact_name", "render"), ARROW_SCHEMA_RENDERERS)
def test_arrow_artifact_is_deterministic(
    artifact_name: str,
    render: Callable[[], str],
) -> None:
    # Given
    artifact = (CONTRACTS_DIRECTORY / artifact_name).read_text(encoding="utf-8")

    # When / Then
    assert artifact == render()


def test_manifest_has_no_arrow_artifact() -> None:
    # Given / When
    artifact_names = {path.name for path in CONTRACTS_DIRECTORY.glob("*.arrow.*")}

    # Then
    assert "article_batch_manifest.v1.arrow.schema.json" not in artifact_names


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
