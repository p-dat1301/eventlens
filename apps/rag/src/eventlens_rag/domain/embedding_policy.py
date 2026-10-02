"""Approved embedding identity contract."""

from typing import Literal

from eventlens_contracts.base import ContractModel, NonEmptyText


class EmbeddingPolicy(ContractModel):
    """Frozen baseline embedding policy; contract does not perform API calls."""

    model: Literal["text-embedding-3-small"]
    dimension: Literal[1536]
    embedding_version: NonEmptyText
