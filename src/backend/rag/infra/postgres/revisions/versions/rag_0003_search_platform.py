"""Search platform: index generations, transactional outbox, embedding cache.

Revision ID: rag_0003_search_platform
Revises: rag_0002_source_versions
Create Date: 2026-10-27

Additive only; no existing table, view, trigger or row changes.

* ``rag_search_generations``: one row per search-index build (chunker version,
  embedding identity/model/dimensions/revision, index name, lifecycle). At most
  one ACTIVE row (partial unique index).
* ``rag_search_outbox``: change events for the derived search projection.
  Triggers on ``rag_article_current`` and ``rag_enrichment_publications``
  insert them in the *same transaction* as the change, for every write path
  (ingest, load-fixtures, correction, tombstone, enrichment publication), so
  an event can never be lost or precede its data. Activation of a search
  generation inserts ``embedding_generation_activated`` in its own
  transaction. Rows are never deleted by the application.
* ``rag_search_embedding_cache``: vectors keyed by ``(text_hash,
  embedding_identity)`` stored as ``double precision[]`` (dimension checked),
  independent of the fixed ``vector(1536)`` column, so BGE-M3 (1024) never
  overwrites the fake vectors.

``downgrade`` drops only these additive objects (no RAG source data).
"""

from collections.abc import Sequence

from alembic import op

from backend.rag.infra.postgres.engine import execute

revision: str = "rag_0003_search_platform"
down_revision: str | Sequence[str] | None = "rag_0002_source_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_TYPES = (
    "'article_revision_created', 'article_revision_updated', "
    "'article_tombstoned', 'enrichment_published', "
    "'embedding_generation_activated'"
)

UPGRADE_DDL: tuple[str, ...] = (
    """CREATE TABLE rag_search_generations (
    generation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    index_name text NOT NULL UNIQUE,
    chunk_version text NOT NULL,
    embedding_identity text,
    embedding_model text,
    embedding_dimensions integer
        CHECK (embedding_dimensions IS NULL OR embedding_dimensions > 0),
    embedding_revision text,
    status text NOT NULL DEFAULT 'BUILDING'
        CHECK (status IN ('BUILDING', 'ACTIVE', 'RETIRED', 'FAILED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    activated_at timestamptz,
    CHECK ((embedding_identity IS NULL) = (embedding_dimensions IS NULL))
)""",
    """CREATE UNIQUE INDEX rag_search_generations_one_active
    ON rag_search_generations ((true)) WHERE status = 'ACTIVE'""",
    f"""CREATE TABLE rag_search_outbox (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type text NOT NULL CHECK (event_type IN ({EVENT_TYPES})),
    article_id uuid,
    content_hash text,
    generation_id bigint REFERENCES rag_search_generations (generation_id),
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_expires_at timestamptz,
    claim_token uuid,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CHECK (article_id IS NOT NULL
        OR event_type = 'embedding_generation_activated')
)""",
    """CREATE INDEX rag_search_outbox_due_idx
    ON rag_search_outbox (status, next_attempt_at, event_id)""",
    """CREATE TABLE rag_search_embedding_cache (
    text_hash text NOT NULL CHECK (text_hash ~ '^sha256:[0-9a-f]{64}$'),
    embedding_identity text NOT NULL,
    embedding_dimensions integer NOT NULL CHECK (embedding_dimensions > 0),
    embedding double precision[] NOT NULL
        CHECK (cardinality(embedding) = embedding_dimensions),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (text_hash, embedding_identity)
)""",
    """CREATE FUNCTION rag_search_outbox_current() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO rag_search_outbox (event_type, article_id, content_hash)
        VALUES (CASE WHEN NEW.tombstoned_at IS NULL
                     THEN 'article_revision_created'
                     ELSE 'article_tombstoned' END,
                NEW.article_id, NEW.content_hash);
    ELSIF NEW.tombstoned_at IS NOT NULL AND OLD.tombstoned_at IS NULL THEN
        INSERT INTO rag_search_outbox (event_type, article_id, content_hash)
        VALUES ('article_tombstoned', NEW.article_id, NEW.content_hash);
    ELSIF NEW.content_hash IS DISTINCT FROM OLD.content_hash THEN
        INSERT INTO rag_search_outbox (event_type, article_id, content_hash)
        VALUES ('article_revision_updated', NEW.article_id, NEW.content_hash);
    END IF;
    RETURN NULL;
END;
$$""",
    """CREATE TRIGGER rag_search_outbox_current
AFTER INSERT OR UPDATE ON rag_article_current
FOR EACH ROW EXECUTE FUNCTION rag_search_outbox_current()""",
    """CREATE FUNCTION rag_search_outbox_publication() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO rag_search_outbox (event_type, article_id, content_hash)
    VALUES ('enrichment_published', NEW.article_id, NEW.content_hash);
    RETURN NULL;
END;
$$""",
    """CREATE TRIGGER rag_search_outbox_publication
AFTER INSERT ON rag_enrichment_publications
FOR EACH ROW EXECUTE FUNCTION rag_search_outbox_publication()""",
)

DOWNGRADE_DDL: tuple[str, ...] = (
    "DROP TRIGGER rag_search_outbox_publication ON rag_enrichment_publications",
    "DROP FUNCTION rag_search_outbox_publication()",
    "DROP TRIGGER rag_search_outbox_current ON rag_article_current",
    "DROP FUNCTION rag_search_outbox_current()",
    "DROP TABLE rag_search_embedding_cache",
    "DROP TABLE rag_search_outbox",
    "DROP TABLE rag_search_generations",
)


def upgrade() -> None:
    """Create the additive search-platform objects."""
    connection = op.get_bind()
    for statement in UPGRADE_DDL:
        _ = execute(connection, statement)


def downgrade() -> None:
    """Drop only the objects this revision added."""
    connection = op.get_bind()
    for statement in DOWNGRADE_DDL:
        _ = execute(connection, statement)
