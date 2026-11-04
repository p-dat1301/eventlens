"""Upstream source-version provenance for RAG article revisions.

Revision ID: rag_0002_source_versions
Revises: rag_0001_baseline
Create Date: 2026-10-27

Adds ``rag_source_versions``: one immutable row per upstream
``article_version_id`` (handoff batch ``eventlens-rag-handoff-v1``), joined to
the RAG revision ``(article_id, content_hash)`` it produced. The upstream
``content_hash`` is stored verbatim next to the RAG NFC hash because the two
hash different normalizations by design. Several upstream versions may map to
one RAG revision (metadata-only upstream edits); one upstream version never maps
to two revisions (primary key). Rows are immutable like article revisions.

Additive only: no existing table, view or index changes, so the baseline
catalog stays as reviewed. ``downgrade`` drops the table only while it is empty.
"""

from collections.abc import Sequence

from alembic import op

from backend.rag.infra.postgres.engine import execute, scalar

revision: str = "rag_0002_source_versions"
down_revision: str | Sequence[str] | None = "rag_0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL: tuple[str, ...] = (
    """CREATE TABLE rag_source_versions (
    article_version_id uuid PRIMARY KEY,
    article_id uuid NOT NULL,
    content_hash text NOT NULL,
    upstream_content_hash text NOT NULL
        CHECK (upstream_content_hash ~ '^[0-9a-f]{64}$'),
    upstream_normalization_version text NOT NULL
        CHECK (upstream_normalization_version <> ''),
    parse_version text NOT NULL CHECK (parse_version <> ''),
    quality_status text NOT NULL CHECK (quality_status IN ('passed', 'warning')),
    export_id text NOT NULL CHECK (export_id <> ''),
    fetched_at timestamptz NOT NULL,
    updated_at_source timestamptz,
    language text NOT NULL CHECK (language <> ''),
    word_count integer NOT NULL CHECK (word_count >= 0),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (article_id, content_hash)
        REFERENCES rag_article_documents (article_id, content_hash)
)""",
    """CREATE INDEX rag_source_versions_revision
ON rag_source_versions (article_id, content_hash)""",
    """CREATE TRIGGER rag_source_version_immutable BEFORE UPDATE OR DELETE
ON rag_source_versions FOR EACH ROW
EXECUTE FUNCTION rag_immutable_revision()""",
)


def upgrade() -> None:
    """Create the provenance table, its revision index and immutability."""
    connection = op.get_bind()
    for statement in UPGRADE_DDL:
        _ = execute(connection, statement)


def downgrade() -> None:
    """Drop the table only when no provenance would be lost."""
    connection = op.get_bind()
    if scalar(execute(connection, "SELECT EXISTS (SELECT FROM rag_source_versions)")):
        msg = "rag_source_versions holds provenance; restore from backup instead"
        raise RuntimeError(msg)
    _ = execute(connection, "DROP TABLE rag_source_versions")
