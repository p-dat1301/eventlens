"""Article media v1 contract."""

from typing import Literal
from uuid import UUID

from pydantic import HttpUrl

from eventlens_contracts.base import (
    ContractModel,
    NonNegativeInt32,
    NonNegativeInt64,
    PrefixedSha256,
    StorageUri,
    UtcDatetime,
)


class ArticleMedia(ContractModel):
    """Media row associated with canonical article."""

    schema_version: Literal["article_media.v1"]
    media_id: UUID
    article_id: UUID
    media_type: Literal["image", "video", "audio", "document"]
    position: NonNegativeInt32
    role: Literal["lead", "content", "gallery", "infographic", "thumbnail", "unknown"]
    source_url: HttpUrl
    canonical_url: HttpUrl | None = None
    storage_uri: StorageUri | None = None
    mime_type: str | None = None
    width: NonNegativeInt32 | None = None
    height: NonNegativeInt32 | None = None
    bytes: NonNegativeInt64 | None = None
    content_hash: PrefixedSha256 | None = None
    caption: str | None = None
    alt_text: str | None = None
    credit: str | None = None
    is_lead: bool
    download_status: Literal["success", "failed", "skipped_policy", "not_attempted"]
    quality_flags: tuple[str, ...]
    collected_at: UtcDatetime
