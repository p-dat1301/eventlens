"""Bounded analyst briefing query contract."""

# ruff: noqa: EM101

from typing import Annotated, Literal, LiteralString, Self
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from eventlens_contracts.base import (
    CanonicalDomain,
    ContractModel,
    NonEmptyText,
    UtcDatetime,
)
from eventlens_contracts.taxonomy import TopicTag

BoundedPositiveInt = Annotated[int, Field(ge=1, le=100)]


def _error(error_type: LiteralString, message: LiteralString) -> PydanticCustomError:
    return PydanticCustomError(error_type, message)


class BriefingQuery(ContractModel):
    """Bounded analyst request over source-publication time."""

    schema_version: Literal["briefing_query.v1"]
    query: NonEmptyText
    source_domains: tuple[CanonicalDomain, ...] = ()
    topic_filters: tuple[TopicTag, ...] = ()
    entity_ids: tuple[UUID, ...] = ()
    published_after: UtcDatetime | None = None
    published_before: UtcDatetime | None = None
    publication_time_basis: Literal["source_published_at"]
    result_limit: BoundedPositiveInt
    context_budget: Annotated[int, Field(ge=1, le=12_000)]

    @model_validator(mode="after")
    def enforce_query_filters(self) -> Self:
        """Reject ambiguous filter sets and invalid publication ranges."""
        if (
            self.published_after is not None
            and self.published_before is not None
            and self.published_after > self.published_before
        ):
            raise _error(
                "invalid_publication_range",
                "published_after must not exceed published_before",
            )
        if len(set(self.source_domains)) != len(self.source_domains):
            raise _error(
                "duplicate_source_domain", "query source domains must be unique"
            )
        if len(set(self.topic_filters)) != len(self.topic_filters):
            raise _error("duplicate_topic_filter", "query topic filters must be unique")
        if len(set(self.entity_ids)) != len(self.entity_ids):
            raise _error("duplicate_entity_id", "query entity ids must be unique")
        return self
