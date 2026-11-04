"""Handoff revisions plus upstream provenance, one transaction per batch.

The revision goes through ``persist_revision`` (the same persist-first path as
ingest), so the ``rag_article_current`` trigger writes the search outbox event
in the same transaction. Re-importing an unchanged version touches nothing
that the trigger reports (same content hash), so no duplicate event is queued.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import Connection, Engine, Text, cast, exists, func, select
from sqlalchemy.dialects.postgresql import insert

from backend.rag.application.ports.source_versions import (
    PersistOutcome,
    ProvenanceConflictError,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.handoff import SourceProvenance
from backend.rag.infra.postgres.engine import transaction
from backend.rag.infra.postgres.models import (
    ArticleCurrentRow,
    ArticleDocumentRow,
    SourceVersionRow,
)
from backend.rag.infra.postgres.store import persist_revision

_ROW = SourceVersionRow
# Wider than SQLAlchemy's typed select overloads; validated explicitly instead.
_PROVENANCE_ROWS: TypeAdapter[
    list[
        tuple[
            UUID,
            str,
            UUID,
            str,
            str,
            str,
            str,
            str,
            datetime,
            datetime | None,
            str,
            int,
        ]
    ]
] = TypeAdapter(
    list[
        tuple[
            UUID,
            str,
            UUID,
            str,
            str,
            str,
            str,
            str,
            datetime,
            datetime | None,
            str,
            int,
        ]
    ]
)


def _identity(provenance: SourceProvenance) -> tuple[object, ...]:
    """Fields one upstream version can never change (conflict check)."""
    return (
        provenance.article_id,
        provenance.rag_content_hash,
        provenance.upstream_content_hash,
        provenance.upstream_normalization_version,
        provenance.parse_version,
        provenance.language,
        provenance.word_count,
    )


def _persist_one(
    connection: Connection, document: ArticleDocument, provenance: SourceProvenance
) -> PersistOutcome:
    existed = bool(
        connection.scalar(
            select(
                exists().where(
                    ArticleDocumentRow.article_id == document.article_id,
                    ArticleDocumentRow.content_hash == document.content_hash,
                )
            )
        )
    )
    # Same text hash must never silently hide changes to source metadata.
    stored = connection.scalar(
        select(cast(ArticleDocumentRow.document_json, Text)).where(
            ArticleDocumentRow.article_id == document.article_id,
            ArticleDocumentRow.content_hash == document.content_hash,
        )
    )
    if stored is not None:
        previous = ArticleDocument.model_validate_json(stored)
        excluded = {"ingested_at", "processed_at", "evidence_spans"}
        if previous.model_dump(exclude=excluded) != document.model_dump(
            exclude=excluded
        ):
            raise ProvenanceConflictError(provenance.article_version_id)
    bound = connection.execute(
        select(
            _ROW.article_id,
            _ROW.content_hash,
            _ROW.upstream_content_hash,
            _ROW.upstream_normalization_version,
            _ROW.parse_version,
            _ROW.language,
            _ROW.word_count,
        ).where(_ROW.article_version_id == provenance.article_version_id)
    ).first()
    if bound is not None:
        if tuple(bound) != _identity(provenance):
            raise ProvenanceConflictError(provenance.article_version_id)
        # Validate before returning, but never promote a replay or stale jobs.
        return PersistOutcome(
            revision_inserted=False,
            provenance_inserted=False,
            current=_is_current(connection, document),
        )
    _require_newer(connection, document, provenance)
    persist_revision(connection, document)
    inserted = (
        connection.execute(
            insert(_ROW)
            .values(
                article_version_id=provenance.article_version_id,
                article_id=provenance.article_id,
                content_hash=provenance.rag_content_hash,
                upstream_content_hash=provenance.upstream_content_hash,
                upstream_normalization_version=provenance.upstream_normalization_version,
                parse_version=provenance.parse_version,
                quality_status=provenance.quality_status,
                export_id=provenance.export_id,
                fetched_at=provenance.fetched_at,
                updated_at_source=provenance.updated_at_source,
                language=provenance.language,
                word_count=provenance.word_count,
            )
            .on_conflict_do_nothing(index_elements=[_ROW.article_version_id])
            .returning(_ROW.article_version_id)
        ).first()
        is not None
    )
    if not inserted:
        bound = connection.execute(
            select(
                _ROW.article_id,
                _ROW.content_hash,
                _ROW.upstream_content_hash,
                _ROW.upstream_normalization_version,
                _ROW.parse_version,
                _ROW.language,
                _ROW.word_count,
            ).where(_ROW.article_version_id == provenance.article_version_id)
        ).first()
        if bound is None or tuple(bound) != _identity(provenance):
            raise ProvenanceConflictError(provenance.article_version_id)
    return PersistOutcome(
        revision_inserted=not existed,
        provenance_inserted=inserted,
        current=_is_current(connection, document),
    )


def _is_current(connection: Connection, document: ArticleDocument) -> bool:
    return bool(
        connection.scalar(
            select(
                exists().where(
                    ArticleCurrentRow.article_id == document.article_id,
                    ArticleCurrentRow.content_hash == document.content_hash,
                    ArticleCurrentRow.tombstoned_at.is_(None),
                )
            )
        )
    )


def _require_newer(
    connection: Connection, document: ArticleDocument, provenance: SourceProvenance
) -> None:
    """Unseen changed text needs strictly newer upstream time, never arrival time.

    Without a comparable upstream history, fail closed instead of replacing a
    revision written through another ingest path. Same-body metadata changes
    are rejected above: they require an explicit metadata revision contract.
    """
    current_hash = connection.scalar(
        select(ArticleCurrentRow.content_hash)
        .where(
            ArticleCurrentRow.article_id == document.article_id,
        )
        .with_for_update()
    )
    if current_hash is None or current_hash == document.content_hash:
        return
    latest = connection.scalar(
        select(
            func.max(
                func.coalesce(
                    _ROW.updated_at_source,
                    _ROW.fetched_at,
                )
            )
        ).where(_ROW.article_id == document.article_id)
    )
    incoming = provenance.updated_at_source or provenance.fetched_at
    if latest is None or incoming <= latest:
        raise ProvenanceConflictError(provenance.article_version_id)


@dataclass(frozen=True, slots=True)
class PostgresSourceVersions:
    """``SourceVersionStore`` over ``rag_source_versions`` (revision 0002)."""

    engine: Engine

    def persist_batch(
        self, items: Sequence[tuple[ArticleDocument, SourceProvenance]]
    ) -> tuple[PersistOutcome, ...]:
        """All revisions + provenance rows of a batch, or none of them.

        Re-importing the same upstream version is a no-op. Binding an upstream
        version to a different revision (or different immutable fields) raises
        ``ProvenanceConflictError`` and rolls the whole batch back.
        """
        with transaction(self.engine) as connection:
            # Sorted transaction locks cover absent rows as well, and serialize
            # concurrent batches without lock-order inversions. Version locks
            # also serialize attempts to bind one version to different articles.
            keys = sorted(
                {
                    key
                    for document, provenance in items
                    for key in (
                        f"handoff:article:{document.article_id}",
                        f"handoff:version:{provenance.article_version_id}",
                    )
                }
            )
            for key in keys:
                _ = connection.scalar(
                    select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
                )
            return tuple(
                _persist_one(connection, document, provenance)
                for document, provenance in items
            )

    def current_documents(self) -> tuple[ArticleDocument, ...]:
        """Current untombstoned revisions, read back from PostgreSQL."""
        with transaction(self.engine, read_only=True) as connection:
            texts = connection.scalars(
                select(cast(ArticleDocumentRow.document_json, Text))
                .join(
                    ArticleCurrentRow,
                    (ArticleCurrentRow.article_id == ArticleDocumentRow.article_id)
                    & (
                        ArticleCurrentRow.content_hash
                        == ArticleDocumentRow.content_hash
                    ),
                )
                .where(
                    ArticleCurrentRow.tombstoned_at.is_(None),
                    ArticleDocumentRow.document_json.is_not(None),
                )
                .order_by(ArticleDocumentRow.article_id)
            ).all()
        # JSON text, not the decoded JSONB dict: the contract model is strict.
        return tuple(ArticleDocument.model_validate_json(text) for text in texts)

    def provenance_for(
        self, article_ids: tuple[UUID, ...]
    ) -> tuple[SourceProvenance, ...]:
        """All recorded upstream versions of the articles, oldest first."""
        with transaction(self.engine, read_only=True) as connection:
            rows = connection.execute(
                select(
                    _ROW.article_id,
                    _ROW.content_hash,
                    _ROW.article_version_id,
                    _ROW.upstream_content_hash,
                    _ROW.upstream_normalization_version,
                    _ROW.parse_version,
                    _ROW.quality_status,
                    _ROW.export_id,
                    _ROW.fetched_at,
                    _ROW.updated_at_source,
                    _ROW.language,
                    _ROW.word_count,
                )
                .where(_ROW.article_id.in_(article_ids))
                .order_by(_ROW.article_id, _ROW.recorded_at, _ROW.article_version_id)
            ).all()
        typed = _PROVENANCE_ROWS.validate_python([tuple(row) for row in rows])
        return tuple(
            SourceProvenance(
                article_id=article_id,
                rag_content_hash=content_hash,
                article_version_id=version_id,
                upstream_content_hash=upstream,
                upstream_normalization_version=normalization,
                parse_version=parse,
                quality_status=quality,
                export_id=export,
                fetched_at=fetched,
                updated_at_source=updated,
                language=language,
                word_count=words,
            )
            for (
                article_id,
                content_hash,
                version_id,
                upstream,
                normalization,
                parse,
                quality,
                export,
                fetched,
                updated,
                language,
                words,
            ) in typed
        )
