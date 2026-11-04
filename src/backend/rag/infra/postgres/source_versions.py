"""Handoff revisions plus upstream provenance in one transaction per record."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import Engine

from backend.rag.application.ports.source_versions import (
    PersistOutcome,
    ProvenanceConflictError,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.handoff import SourceProvenance
from backend.rag.infra.postgres.engine import execute, scalar, scalars, transaction
from backend.rag.infra.postgres.store import persist_revision

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
_BOUND: TypeAdapter[tuple[UUID, str, str] | None] = TypeAdapter(
    tuple[UUID, str, str] | None
)

_COLUMNS = """article_id, content_hash, article_version_id, upstream_content_hash,
    upstream_normalization_version, parse_version, quality_status, export_id,
    fetched_at, updated_at_source, language, word_count"""


@dataclass(frozen=True, slots=True)
class PostgresSourceVersions:
    """``SourceVersionStore`` over ``rag_source_versions`` (revision 0002)."""

    engine: Engine

    def persist_with_provenance(
        self, document: ArticleDocument, provenance: SourceProvenance
    ) -> PersistOutcome:
        """Persist revision + lexical pointer, then bind the upstream version.

        Re-importing the same upstream version is a no-op. Binding an upstream
        version to a different revision raises and rolls the record back.
        """
        with transaction(self.engine) as connection:
            existed = bool(
                scalar(
                    execute(
                        connection,
                        """SELECT EXISTS (SELECT FROM rag_article_documents
                        WHERE article_id = %s AND content_hash = %s)""",
                        (document.article_id, document.content_hash),
                    )
                )
            )
            persist_revision(connection, document)
            inserted = (
                execute(
                    connection,
                    f"""INSERT INTO rag_source_versions ({_COLUMNS})
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (article_version_id) DO NOTHING
                    RETURNING article_version_id""",  # noqa: S608 - constant columns
                    (
                        provenance.article_id,
                        provenance.rag_content_hash,
                        provenance.article_version_id,
                        provenance.upstream_content_hash,
                        provenance.upstream_normalization_version,
                        provenance.parse_version,
                        provenance.quality_status,
                        provenance.export_id,
                        provenance.fetched_at,
                        provenance.updated_at_source,
                        provenance.language,
                        provenance.word_count,
                    ),
                ).fetchone()
                is not None
            )
            if not inserted:
                bound = _BOUND.validate_python(
                    execute(
                        connection,
                        """SELECT article_id, content_hash, upstream_content_hash
                        FROM rag_source_versions WHERE article_version_id = %s""",
                        (provenance.article_version_id,),
                    ).fetchone()
                )
                if bound != (
                    provenance.article_id,
                    provenance.rag_content_hash,
                    provenance.upstream_content_hash,
                ):
                    raise ProvenanceConflictError(provenance.article_version_id)
            current = bool(
                scalar(
                    execute(
                        connection,
                        """SELECT EXISTS (SELECT FROM rag_article_current
                        WHERE article_id = %s AND content_hash = %s
                          AND tombstoned_at IS NULL)""",
                        (document.article_id, document.content_hash),
                    )
                )
            )
        return PersistOutcome(
            revision_inserted=not existed,
            provenance_inserted=inserted,
            current=current,
        )

    def current_documents(self) -> tuple[ArticleDocument, ...]:
        """Current untombstoned revisions (``rag_current_lexical``)."""
        with transaction(self.engine, read_only=True) as connection:
            texts = TypeAdapter(list[str]).validate_python(
                scalars(
                    execute(
                        connection,
                        """SELECT d.document_json::text FROM rag_current_lexical d
                        WHERE d.document_json IS NOT NULL ORDER BY d.article_id""",
                    )
                )
            )
        return tuple(ArticleDocument.model_validate_json(text) for text in texts)

    def provenance_for(
        self, article_ids: tuple[UUID, ...]
    ) -> tuple[SourceProvenance, ...]:
        """All recorded upstream versions of the articles, oldest first."""
        with transaction(self.engine, read_only=True) as connection:
            rows = _PROVENANCE_ROWS.validate_python(
                [
                    tuple(row)
                    for row in execute(
                        connection,
                        f"""SELECT {_COLUMNS} FROM rag_source_versions
                        WHERE article_id = ANY(%s)
                        ORDER BY article_id, recorded_at, article_version_id""",  # noqa: S608
                        (list(article_ids),),
                    )
                ]
            )
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
            ) in rows
        )
