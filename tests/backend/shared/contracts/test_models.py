import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.shared.contracts import (
    Article,
    ArticleBatchManifest,
    ArticleMedia,
    ArticleRejected,
)

FIXTURE_PATH = Path(__file__).parents[4] / "fixtures/article_bundle.v1.json"
REJECTED_FIXTURE_PATH = Path(__file__).parents[4] / "fixtures/article_rejected.v1.json"
MANIFEST_FIXTURE_PATH = (
    Path(__file__).parents[4] / "fixtures/article_batch_manifest.v1.json"
)


def test_article_contract_has_exact_frozen_fields() -> None:
    # Given / When
    fields = set(Article.model_fields)

    # Then
    assert fields == {
        "schema_version",
        "article_id",
        "source_id",
        "source",
        "canonical_url",
        "title",
        "content",
        "summary_source",
        "author",
        "section",
        "tags",
        "published_at",
        "updated_at_source",
        "lead_image_url",
        "lead_image_storage_uri",
        "media_count",
        "language",
        "word_count",
        "content_hash",
        "duplicate",
        "raw",
        "processing",
    }


def test_media_contract_has_exact_frozen_fields() -> None:
    # Given / When
    fields = set(ArticleMedia.model_fields)

    # Then
    assert fields == {
        "schema_version",
        "media_id",
        "article_id",
        "media_type",
        "position",
        "role",
        "source_url",
        "canonical_url",
        "storage_uri",
        "mime_type",
        "width",
        "height",
        "bytes",
        "content_hash",
        "caption",
        "alt_text",
        "credit",
        "is_lead",
        "download_status",
        "quality_flags",
        "collected_at",
    }


def test_rejected_record_validates_exact_fixture() -> None:
    # Given
    fixture_json = REJECTED_FIXTURE_PATH.read_text(encoding="utf-8")

    # When
    rejected = ArticleRejected.model_validate_json(fixture_json)

    # Then
    assert rejected.reason_code == "non_article"
    assert len(ArticleRejected.model_fields) == 8


def test_manifest_validates_when_quality_totals_match() -> None:
    # Given
    fixture_json = MANIFEST_FIXTURE_PATH.read_text(encoding="utf-8")

    # When
    manifest = ArticleBatchManifest.model_validate_json(fixture_json)

    # Then
    assert manifest.schema_version == "article_batch_manifest.v1"


def test_manifest_rejects_quality_total_mismatch() -> None:
    # Given
    fixture_json = MANIFEST_FIXTURE_PATH.read_text(encoding="utf-8").replace(
        '"input": 136', '"input": 137'
    )

    # When / Then
    with pytest.raises(ValidationError, match=r"quality\.input"):
        _ = ArticleBatchManifest.model_validate_json(fixture_json)


def test_int32_contract_rejects_arrow_overflow() -> None:
    # Given
    fixture_json = FIXTURE_PATH.read_text(encoding="utf-8")
    bounded_json = fixture_json.replace('"width": 1200', '"width": 2147483647')
    overflow_json = bounded_json.replace(
        '"width": 2147483647',
        '"width": 2147483648',
    )

    # When / Then
    assert (
        ArticleMedia.model_validate_json(
            json.dumps(json.loads(bounded_json)["media"][0])
        ).width
        == 2_147_483_647
    )
    with pytest.raises(ValidationError, match="less than or equal"):
        _ = ArticleMedia.model_validate_json(
            json.dumps(json.loads(overflow_json)["media"][0])
        )


def test_int64_contract_rejects_arrow_overflow() -> None:
    # Given
    fixture_json = FIXTURE_PATH.read_text(encoding="utf-8")
    bounded_json = fixture_json.replace(
        '"bytes": 182440',
        '"bytes": 9223372036854775807',
    )
    overflow_json = bounded_json.replace(
        '"bytes": 9223372036854775807',
        '"bytes": 9223372036854775808',
    )

    # When / Then
    assert ArticleMedia.model_validate_json(
        json.dumps(json.loads(bounded_json)["media"][0])
    ).bytes == (9_223_372_036_854_775_807)
    with pytest.raises(ValidationError, match="less than or equal"):
        _ = ArticleMedia.model_validate_json(
            json.dumps(json.loads(overflow_json)["media"][0])
        )
