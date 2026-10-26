"""Shared strict types for EventLens contracts."""

from datetime import datetime
from typing import Annotated, ClassVar

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

StorageUri = Annotated[str, Field(pattern=r"^s3://[^/\s]+/.+$")]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeInt32 = Annotated[int, Field(ge=0, le=2_147_483_647)]
NonNegativeInt64 = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]
HttpStatus = Annotated[int, Field(ge=100, le=599)]
PrefixedSha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CanonicalDomain = Annotated[str, Field(pattern=r"^[a-z0-9.-]+$")]


def _require_non_blank(value: str) -> str:
    if not value.strip():
        message = "text must not be blank"
        raise ValueError(message)
    return value


NonEmptyText = Annotated[
    str, Field(min_length=1, pattern=r"\S"), AfterValidator(_require_non_blank)
]


def _require_utc(value: datetime) -> datetime:
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        message = "timestamp must use UTC"
        raise ValueError(message)
    return value


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_require_utc)]


class ContractModel(BaseModel):
    """Strict immutable base for external contracts."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True
    )
