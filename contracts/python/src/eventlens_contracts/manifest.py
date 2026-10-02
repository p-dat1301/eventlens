"""Article batch manifest v1 contract."""

from typing import Literal, Self
from uuid import UUID

from pydantic import model_validator
from pydantic_core import PydanticCustomError

from eventlens_contracts.base import (
    ContractModel,
    NonEmptyText,
    NonNegativeInt,
    Sha256,
    StorageUri,
    UtcDatetime,
)

QUALITY_INPUT_MISMATCH = "quality_input_mismatch"


class ManifestFile(ContractModel):
    """Published file metadata."""

    uri: StorageUri
    rows: NonNegativeInt
    bytes: NonNegativeInt
    sha256: Sha256


class ManifestQuality(ContractModel):
    """Batch quality counters."""

    input: NonNegativeInt
    passed: NonNegativeInt
    flagged: NonNegativeInt
    rejected: NonNegativeInt
    duplicates: NonNegativeInt

    @model_validator(mode="after")
    def enforce_input_total(self) -> Self:
        """Require input count to equal all quality outcomes."""
        if self.input != self.passed + self.flagged + self.rejected:
            message = "quality.input must equal passed + flagged + rejected"
            raise PydanticCustomError(QUALITY_INPUT_MISMATCH, message)
        return self


class ArticleBatchManifest(ContractModel):
    """Published Silver batch handoff manifest."""

    schema_version: Literal["article_batch_manifest.v1"]
    batch_id: UUID
    source: NonEmptyText
    partition: NonEmptyText
    files: tuple[ManifestFile, ...]
    quality: ManifestQuality
    parse_versions: tuple[str, ...]
    created_at: UtcDatetime
    supersedes_batch_id: UUID | None = None
