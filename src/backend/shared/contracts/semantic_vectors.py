"""Typed reader for language-neutral semantic test vectors."""

from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict


class Replacement(BaseModel):
    """Literal fixture substitution."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True
    )
    old: str
    new: str


class SemanticVector(BaseModel):
    """Expected validation outcome for one fixture."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True
    )
    name: str
    contract: Literal[
        "article_bundle.v1",
        "article_batch_manifest.v1",
        "event.v1",
        "article_document.v1",
        "article_enrichment.v1",
    ]
    fixture: str
    replacements: tuple[Replacement, ...]
    valid: bool
    error_type: str | None


class SemanticVectors(BaseModel):
    """Versioned vector artifact."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True
    )
    schema_version: Literal["eventlens_semantic_vectors.v1"]
    vectors: tuple[SemanticVector, ...]


def load_semantic_vectors(root: Path) -> tuple[SemanticVector, ...]:
    """Parse checked-in semantic vectors once at boundary."""
    artifact = (
        root / "src/backend/shared/contracts/semantic_vectors.v1.json"
    ).read_text(encoding="utf-8")
    return SemanticVectors.model_validate_json(artifact).vectors
