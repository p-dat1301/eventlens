"""Article rejected v1 contract."""

from typing import Literal
from uuid import UUID

from eventlens_contracts.base import (
    ContractModel,
    NonEmptyText,
    StorageUri,
    UtcDatetime,
)


class ArticleRejected(ContractModel):
    """Raw article rejected by extraction quality gate."""

    schema_version: Literal["article_rejected.v1"]
    article_raw_id: UUID
    source_id: UUID
    raw_storage_uri: StorageUri
    reason_code: NonEmptyText
    error_detail: str | None = None
    parse_version: NonEmptyText
    rejected_at: UtcDatetime
