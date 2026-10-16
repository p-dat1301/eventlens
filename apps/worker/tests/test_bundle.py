from pathlib import Path

import pytest
from pydantic import ValidationError

from eventlens_worker.bundle import ArticleBundle

FIXTURE_PATH = Path(__file__).parents[3] / "fixtures/article_bundle.v1.json"
SECOND_MEDIA_ARTICLE = """"article_id": "550e8400-e29b-41d4-a716-446655440000",
      "media_type": "image",
      "position": 1"""
UNRELATED_MEDIA_ARTICLE = """"article_id": "216f0e33-25c4-49af-af92-cc4730896c21",
      "media_type": "image",
      "position": 1"""


def test_bundle_validates_when_article_and_media_match() -> None:
    fixture_json = FIXTURE_PATH.read_text(encoding="utf-8")
    bundle = ArticleBundle.model_validate_json(fixture_json)
    assert bundle.article.media_count == 2
    assert len(bundle.media) == 2
    assert all(item.article_id == bundle.article.article_id for item in bundle.media)


def test_bundle_rejects_media_count_mismatch() -> None:
    fixture_json = FIXTURE_PATH.read_text(encoding="utf-8").replace(
        '"media_count": 2', '"media_count": 3'
    )
    with pytest.raises(ValidationError, match="media_count"):
        _ = ArticleBundle.model_validate_json(fixture_json)


def test_bundle_rejects_unrelated_media() -> None:
    fixture_json = FIXTURE_PATH.read_text(encoding="utf-8").replace(
        SECOND_MEDIA_ARTICLE, UNRELATED_MEDIA_ARTICLE
    )
    with pytest.raises(ValidationError, match="article_id"):
        _ = ArticleBundle.model_validate_json(fixture_json)


def test_bundle_rejects_duplicate_media_ids() -> None:
    fixture_json = FIXTURE_PATH.read_text(encoding="utf-8").replace(
        '"media_id": "326f2fcf-5485-4f9c-ab3c-d54bbbcd0d85"',
        '"media_id": "6f89d402-18e4-45f8-9343-2aa80a5693b4"',
    )
    with pytest.raises(ValidationError, match="media_id"):
        _ = ArticleBundle.model_validate_json(fixture_json)


def test_bundle_rejects_non_contiguous_media_positions() -> None:
    fixture_json = FIXTURE_PATH.read_text(encoding="utf-8").replace(
        '"position": 1', '"position": 2'
    )
    with pytest.raises(ValidationError, match="position"):
        _ = ArticleBundle.model_validate_json(fixture_json)
