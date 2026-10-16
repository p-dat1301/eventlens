"""Cross-artifact article bundle validation."""

from typing import Self

from pydantic import model_validator
from pydantic_core import PydanticCustomError

from eventlens_contracts.article import Article
from eventlens_contracts.base import ContractModel
from eventlens_contracts.media import ArticleMedia

MEDIA_COUNT_MISMATCH = "media_count_mismatch"
ARTICLE_ID_MISMATCH = "article_id_mismatch"
DUPLICATE_MEDIA_ID = "duplicate_media_id"
NON_CONTIGUOUS_POSITION = "non_contiguous_position"


class ArticleBundle(ContractModel):
    """Canonical article plus complete active media rows."""

    article: Article
    media: tuple[ArticleMedia, ...]

    @model_validator(mode="after")
    def enforce_bundle_invariants(self) -> Self:
        """Reject incomplete or internally inconsistent bundles."""
        if self.article.media_count != len(self.media):
            message = "media_count must equal number of media items"
            raise PydanticCustomError(MEDIA_COUNT_MISMATCH, message)
        if any(item.article_id != self.article.article_id for item in self.media):
            message = "media article_id must match article article_id"
            raise PydanticCustomError(ARTICLE_ID_MISMATCH, message)
        media_ids = tuple(item.media_id for item in self.media)
        if len(set(media_ids)) != len(media_ids):
            message = "media_id values must be unique"
            raise PydanticCustomError(DUPLICATE_MEDIA_ID, message)
        positions = sorted(item.position for item in self.media)
        if positions != list(range(len(self.media))):
            message = "media position values must be contiguous from zero"
            raise PydanticCustomError(NON_CONTIGUOUS_POSITION, message)
        return self
