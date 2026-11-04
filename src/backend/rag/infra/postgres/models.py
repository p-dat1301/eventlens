"""SQLAlchemy ORM mapping of the RAG business tables (007 baseline + revision 0002).

These classes describe the existing PostgreSQL schema for adapters and for
Alembic's drift check; they never leave ``rag.infra``. Repositories convert
rows to domain types. Views, triggers, the trigger function and the legacy
``rag_schema_migrations`` ledger are created by the reviewed baseline
revision; runtime code never builds tables from this metadata.
Constraint and index names match the names PostgreSQL generated for the
historical SQL so the mapping compares cleanly against live databases.
"""

from datetime import datetime
from typing import final
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.rag.infra.postgres.types import Vector

_REVISION = ("article_id", "content_hash")
_REVISION_TARGET = (
    "rag_article_documents.article_id",
    "rag_article_documents.content_hash",
)
_CLOCK = text("clock_timestamp()")
_SEARCH_VECTOR = (
    "to_tsvector('simple'::regconfig, ((title || ' '::text) || normalized_content))"
)
_JOB_STATUSES = (
    "status = ANY (ARRAY["
    + ", ".join(
        f"'{status}'::text"
        for status in ("PENDING", "PROCESSING", "READY", "FAILED", "STALE")
    )
    + "])"
)
_NOW = text("now()")


class Base(DeclarativeBase):
    """Declarative base owning the RAG metadata."""


def _tz() -> DateTime:
    return DateTime(timezone=True)


def _uuid() -> PG_UUID[UUID]:
    return PG_UUID(as_uuid=True)


@final
class ArticleDocumentRow(Base):
    """Immutable article revision with its generated FTS vector."""

    __tablename__: str = "rag_article_documents"

    article_id: Mapped[UUID] = mapped_column(_uuid(), primary_key=True)
    content_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[UUID] = mapped_column(_uuid())
    title: Mapped[str] = mapped_column(Text)
    normalized_content: Mapped[str] = mapped_column(Text)
    source_published_at: Mapped[datetime | None] = mapped_column(_tz())
    tombstoned_at: Mapped[datetime | None] = mapped_column(_tz())
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(_SEARCH_VECTOR, persisted=True),
    )
    created_at: Mapped[datetime] = mapped_column(_tz(), server_default=_NOW)
    document_json: Mapped[str | None] = mapped_column(JSONB)

    __table_args__: tuple[object, ...] = (
        Index("rag_article_documents_fts_idx", "search_vector", postgresql_using="gin"),
    )


@final
class ArticleCurrentRow(Base):
    """Current revision pointer, tombstone and active index generation."""

    __tablename__: str = "rag_article_current"

    article_id: Mapped[UUID] = mapped_column(_uuid(), primary_key=True)
    content_hash: Mapped[str] = mapped_column(Text)
    active_index_generation: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("rag_index_generations.generation", name="rag_active_generation_fk"),
    )
    tombstoned_at: Mapped[datetime | None] = mapped_column(_tz())

    __table_args__: tuple[object, ...] = (
        ForeignKeyConstraint(
            _REVISION,
            _REVISION_TARGET,
            name="rag_article_current_article_id_content_hash_fkey",
        ),
    )


@final
class EnrichmentJobRow(Base):
    """Enrichment job: status, attempt budget, lease and claim fence."""

    __tablename__: str = "rag_enrichment_jobs"

    job_id: Mapped[UUID] = mapped_column(_uuid(), primary_key=True)
    article_id: Mapped[UUID] = mapped_column(_uuid())
    content_hash: Mapped[str] = mapped_column(Text)
    enrichment_version: Mapped[str] = mapped_column(Text)
    model_version: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'PENDING'::text"))
    claim_generation: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    lease_expires_at: Mapped[datetime | None] = mapped_column(_tz())
    created_at: Mapped[datetime] = mapped_column(_tz(), server_default=_NOW)
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(_tz(), server_default=_CLOCK)
    failure_code: Mapped[str | None] = mapped_column(Text)
    retry_policy_id: Mapped[str | None] = mapped_column(Text)
    cancel_requested_generation: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(_tz(), server_default=_CLOCK)

    __table_args__: tuple[object, ...] = (
        UniqueConstraint(
            "article_id",
            "content_hash",
            "enrichment_version",
            "model_version",
            "prompt_version",
            name="rag_enrichment_jobs_article_id_content_hash_enrichment_vers_key",
        ),
        ForeignKeyConstraint(
            _REVISION,
            _REVISION_TARGET,
            name="rag_enrichment_jobs_article_id_content_hash_fkey",
        ),
        CheckConstraint(_JOB_STATUSES, name="rag_job_status_check"),
        CheckConstraint(
            "attempt_count >= 0", name="rag_enrichment_jobs_attempt_count_check"
        ),
        Index(
            "rag_enrichment_jobs_claim_idx",
            "status",
            "lease_expires_at",
            "claim_generation",
        ),
        Index(
            "rag_enrichment_jobs_eligible_idx",
            "status",
            "next_attempt_at",
            "created_at",
        ),
    )


@final
class IndexGenerationRow(Base):
    """One staged/active vector generation of one exact article revision."""

    __tablename__: str = "rag_index_generations"

    generation: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    embedding_model: Mapped[str] = mapped_column(Text)
    embedding_version: Mapped[str] = mapped_column(Text)
    embedding_dimension: Mapped[int] = mapped_column(Integer)
    chunker_version: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'STAGING'::text"))
    indexed_at: Mapped[datetime] = mapped_column(_tz(), server_default=_NOW)
    article_id: Mapped[UUID | None] = mapped_column(_uuid())
    content_hash: Mapped[str | None] = mapped_column(Text)
    normalization_version: Mapped[str] = mapped_column(
        Text, server_default=text("'nfc.v1'::text")
    )
    corpus_generation: Mapped[str] = mapped_column(
        Text, server_default=text("'rag-fixture-corpus.v1'::text")
    )
    expected_chunks: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    __table_args__: tuple[object, ...] = (
        CheckConstraint(
            "embedding_dimension = 1536",
            name="rag_index_generations_embedding_dimension_check",
        ),
        ForeignKeyConstraint(
            _REVISION, _REVISION_TARGET, name="rag_generation_revision_fk"
        ),
        Index(
            "rag_generation_identity",
            "article_id",
            "content_hash",
            "normalization_version",
            "chunker_version",
            "embedding_model",
            "embedding_version",
            "embedding_dimension",
            "corpus_generation",
            unique=True,
        ),
    )


@final
class ChunkRow(Base):
    """One chunk span of a generation with its 1536-dimension embedding."""

    __tablename__: str = "rag_chunks"

    chunk_id: Mapped[UUID] = mapped_column(_uuid(), primary_key=True)
    article_id: Mapped[UUID] = mapped_column(_uuid())
    content_hash: Mapped[str] = mapped_column(Text)
    generation: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "rag_index_generations.generation", name="rag_chunks_generation_fkey"
        ),
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Text)
    embedding: Mapped[str] = mapped_column(Vector())

    __table_args__: tuple[object, ...] = (
        CheckConstraint("start_offset >= 0", name="rag_chunks_start_offset_check"),
        CheckConstraint("end_offset > start_offset", name="rag_chunks_check"),
        UniqueConstraint(
            "article_id",
            "content_hash",
            "generation",
            "chunk_index",
            name="rag_chunks_article_id_content_hash_generation_chunk_index_key",
        ),
        ForeignKeyConstraint(
            _REVISION,
            _REVISION_TARGET,
            name="rag_chunks_article_id_content_hash_fkey",
        ),
        Index(
            "rag_chunks_embedding_hnsw_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


@final
class EnrichmentVersionRow(Base):
    """Current published metadata row of one job (deleted on requeue)."""

    __tablename__: str = "rag_enrichment_versions"

    job_id: Mapped[UUID] = mapped_column(
        _uuid(),
        ForeignKey(
            "rag_enrichment_jobs.job_id", name="rag_enrichment_versions_job_id_fkey"
        ),
        primary_key=True,
    )
    generation: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "rag_index_generations.generation",
            name="rag_enrichment_versions_generation_fkey",
        ),
    )
    metadata_json: Mapped[str] = mapped_column("metadata", JSONB)
    published_at: Mapped[datetime] = mapped_column(_tz(), server_default=_CLOCK)


@final
class EnrichmentPublicationRow(Base):
    """Append-only fenced publication history (immutable by trigger)."""

    __tablename__: str = "rag_enrichment_publications"

    publication_id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    job_id: Mapped[UUID] = mapped_column(
        _uuid(),
        ForeignKey(
            "rag_enrichment_jobs.job_id",
            name="rag_enrichment_publications_job_id_fkey",
        ),
    )
    claim_generation: Mapped[int] = mapped_column(BigInteger)
    index_generation: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "rag_index_generations.generation",
            name="rag_enrichment_publications_index_generation_fkey",
        ),
    )
    article_id: Mapped[UUID] = mapped_column(_uuid())
    content_hash: Mapped[str] = mapped_column(Text)
    enrichment_version: Mapped[str] = mapped_column(Text)
    model_version: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column("metadata", JSONB)
    published_at: Mapped[datetime] = mapped_column(_tz(), server_default=_CLOCK)

    __table_args__: tuple[object, ...] = (
        UniqueConstraint(
            "job_id",
            "claim_generation",
            name="rag_enrichment_publications_job_id_claim_generation_key",
        ),
        ForeignKeyConstraint(
            _REVISION,
            _REVISION_TARGET,
            name="rag_enrichment_publications_article_id_content_hash_fkey",
        ),
    )


@final
class SourceReferenceRow(Base):
    """Caller-bound citation reference keyed by the SHA-256 of its token."""

    __tablename__: str = "rag_source_references"

    token_sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    principal_id: Mapped[str] = mapped_column(Text)
    scope_id: Mapped[str] = mapped_column(Text)
    article_id: Mapped[UUID] = mapped_column(_uuid())
    content_hash: Mapped[str] = mapped_column(Text)
    span_id: Mapped[UUID] = mapped_column(_uuid())
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    excerpt: Mapped[str] = mapped_column(Text)
    span_fingerprint: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_tz(), server_default=_CLOCK)
    expires_at: Mapped[datetime] = mapped_column(_tz())

    __table_args__: tuple[object, ...] = (
        CheckConstraint(
            "token_sha256 ~ '^[0-9a-f]{64}$'::text",
            name="rag_source_references_token_sha256_check",
        ),
        CheckConstraint(
            "start_offset >= 0", name="rag_source_references_start_offset_check"
        ),
        CheckConstraint(
            "end_offset > start_offset", name="rag_source_references_check"
        ),
        CheckConstraint("expires_at > created_at", name="rag_source_references_check1"),
        ForeignKeyConstraint(
            _REVISION,
            _REVISION_TARGET,
            name="rag_source_references_article_id_content_hash_fkey",
        ),
        Index("rag_source_references_expiry_idx", "expires_at"),
    )


@final
class SourceVersionRow(Base):
    """Upstream handoff version bound to one RAG revision (revision 0002)."""

    __tablename__: str = "rag_source_versions"

    article_version_id: Mapped[UUID] = mapped_column(_uuid(), primary_key=True)
    article_id: Mapped[UUID] = mapped_column(_uuid())
    content_hash: Mapped[str] = mapped_column(Text)
    upstream_content_hash: Mapped[str] = mapped_column(Text)
    upstream_normalization_version: Mapped[str] = mapped_column(Text)
    parse_version: Mapped[str] = mapped_column(Text)
    quality_status: Mapped[str] = mapped_column(Text)
    export_id: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(_tz())
    updated_at_source: Mapped[datetime | None] = mapped_column(_tz())
    language: Mapped[str] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(Integer)
    recorded_at: Mapped[datetime] = mapped_column(_tz(), server_default=_NOW)

    __table_args__: tuple[object, ...] = (
        CheckConstraint(
            "upstream_content_hash ~ '^[0-9a-f]{64}$'::text",
            name="rag_source_versions_upstream_content_hash_check",
        ),
        CheckConstraint(
            "quality_status = ANY (ARRAY['passed'::text, 'warning'::text])",
            name="rag_source_versions_quality_status_check",
        ),
        CheckConstraint("word_count >= 0", name="rag_source_versions_word_count_check"),
        ForeignKeyConstraint(
            _REVISION,
            _REVISION_TARGET,
            name="rag_source_versions_article_id_content_hash_fkey",
        ),
        Index("rag_source_versions_revision", "article_id", "content_hash"),
    )
