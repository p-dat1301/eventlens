"""Baseline: the RAG schema exactly as historical SQL 001-007 leaves it.

Revision ID: rag_0001_baseline
Revises:
Create Date: 2026-10-26

Fresh databases: ``upgrade`` builds, in one transaction, the post-007 state
(extension, nine tables in their historical column order, generated FTS
column, identities, CHECK/UNIQUE/FK constraints, GIN and HNSW indexes, the
two current-revision views, the immutability function and triggers) plus the
read-only ``rag_schema_migrations`` audit ledger holding the seven original
checksums. It is reviewed DDL, not autogenerate output; the catalog test
compares the result with a database built by the historical runner.

Databases already built by the historical runner are never upgraded through
this revision: ``eventlens-rag adopt-legacy`` verifies them and only stamps
it. ``downgrade`` is refused because it could only destroy data.
"""

from collections.abc import Sequence

from alembic import op

from backend.rag.infra.postgres.engine import execute

revision: str = "rag_0001_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Raw-byte SHA-256 of the historical scripts; equal to the runner's ledger.
LEGACY_CHECKSUMS: tuple[tuple[str, str], ...] = (
    (
        "001_enable_vector.sql",
        "9e9b2cfec47519f49ee73cb533c459e22f8ca54fe5ba1cbec59f3d5883fe191c",
    ),
    (
        "002_create_rag_index.sql",
        "e458d8238ea5c833982005130be61d61ed672296e0c2014aeaf9178d8fce25a2",
    ),
    (
        "003_storage_guards.sql",
        "08fa6ff5f6936dee68bc7d7e58e48786d1e42a0e427a9a7ef3725413a5bb6b6b",
    ),
    (
        "004_staged_publication.sql",
        "4f373bd275d8ab928b833546aa7be183eb5d0ba33b2e8f110a96ed2495e6f13c",
    ),
    (
        "005_current_lexical_document.sql",
        "c05a1a27301f5ab4bcd084092d1d3219a6f5b77a0f6e1ba3a62bf52218042f0c",
    ),
    (
        "006_enrichment_lifecycle.sql",
        "ce3631be5287f4a5d2d077b65686f50be09dd0f8393b831fa086d2e2c18ef5ca",
    ),
    (
        "007_source_references.sql",
        "1ad7fb7ab49b6c1b4cf09d85938ab43ade08cab4c10ad5e73e66da202f6ddf9e",
    ),
)

# Column order and constraint spelling follow the historical scripts, so
# PostgreSQL derives the same generated names (e.g. rag_chunks_check) and
# ``document.*`` views expand to the same column list.
BASELINE_DDL: tuple[str, ...] = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    """CREATE TABLE rag_article_documents (
    article_id uuid NOT NULL,
    content_hash text NOT NULL,
    source_id uuid NOT NULL,
    title text NOT NULL,
    normalized_content text NOT NULL,
    source_published_at timestamptz,
    tombstoned_at timestamptz,
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', title || ' ' || normalized_content)
    ) STORED,
    created_at timestamptz NOT NULL DEFAULT now(),
    document_json jsonb,
    PRIMARY KEY (article_id, content_hash)
)""",
    """CREATE TABLE rag_index_generations (
    generation bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    embedding_model text NOT NULL,
    embedding_version text NOT NULL,
    embedding_dimension integer NOT NULL CHECK (embedding_dimension = 1536),
    chunker_version text NOT NULL,
    status text NOT NULL DEFAULT 'STAGING',
    indexed_at timestamptz NOT NULL DEFAULT now(),
    article_id uuid,
    content_hash text,
    normalization_version text NOT NULL DEFAULT 'nfc.v1',
    corpus_generation text NOT NULL DEFAULT 'rag-fixture-corpus.v1',
    expected_chunks integer NOT NULL DEFAULT 0,
    CONSTRAINT rag_generation_revision_fk FOREIGN KEY (article_id, content_hash)
        REFERENCES rag_article_documents (article_id, content_hash)
)""",
    """CREATE TABLE rag_article_current (
    article_id uuid PRIMARY KEY,
    content_hash text NOT NULL,
    active_index_generation bigint,
    tombstoned_at timestamptz,
    FOREIGN KEY (article_id, content_hash)
        REFERENCES rag_article_documents (article_id, content_hash),
    CONSTRAINT rag_active_generation_fk FOREIGN KEY (active_index_generation)
        REFERENCES rag_index_generations (generation)
)""",
    """CREATE TABLE rag_enrichment_jobs (
    job_id uuid PRIMARY KEY,
    article_id uuid NOT NULL,
    content_hash text NOT NULL,
    enrichment_version text NOT NULL,
    model_version text NOT NULL,
    prompt_version text NOT NULL,
    status text NOT NULL DEFAULT 'PENDING',
    claim_generation bigint NOT NULL DEFAULT 0,
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    failure_code text,
    retry_policy_id text,
    cancel_requested_generation bigint,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (article_id, content_hash, enrichment_version, model_version,
        prompt_version),
    FOREIGN KEY (article_id, content_hash)
        REFERENCES rag_article_documents (article_id, content_hash),
    CONSTRAINT rag_job_status_check
        CHECK (status IN ('PENDING', 'PROCESSING', 'READY', 'FAILED', 'STALE'))
)""",
    """CREATE TABLE rag_chunks (
    chunk_id uuid PRIMARY KEY,
    article_id uuid NOT NULL,
    content_hash text NOT NULL,
    generation bigint NOT NULL REFERENCES rag_index_generations (generation),
    chunk_index integer NOT NULL,
    start_offset integer NOT NULL CHECK (start_offset >= 0),
    end_offset integer NOT NULL CHECK (end_offset > start_offset),
    content text NOT NULL,
    fingerprint text NOT NULL,
    embedding vector(1536) NOT NULL,
    UNIQUE (article_id, content_hash, generation, chunk_index),
    FOREIGN KEY (article_id, content_hash)
        REFERENCES rag_article_documents (article_id, content_hash)
)""",
    """CREATE TABLE rag_enrichment_versions (
    job_id uuid PRIMARY KEY REFERENCES rag_enrichment_jobs (job_id),
    generation bigint NOT NULL REFERENCES rag_index_generations (generation),
    metadata jsonb NOT NULL,
    published_at timestamptz NOT NULL DEFAULT clock_timestamp()
)""",
    """CREATE TABLE rag_enrichment_publications (
    publication_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES rag_enrichment_jobs (job_id),
    claim_generation bigint NOT NULL,
    index_generation bigint NOT NULL REFERENCES rag_index_generations (generation),
    article_id uuid NOT NULL,
    content_hash text NOT NULL,
    enrichment_version text NOT NULL,
    model_version text NOT NULL,
    prompt_version text NOT NULL,
    metadata jsonb NOT NULL,
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (job_id, claim_generation),
    FOREIGN KEY (article_id, content_hash)
        REFERENCES rag_article_documents (article_id, content_hash)
)""",
    """CREATE TABLE rag_source_references (
    token_sha256 text PRIMARY KEY CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    principal_id text NOT NULL,
    scope_id text NOT NULL,
    article_id uuid NOT NULL,
    content_hash text NOT NULL,
    span_id uuid NOT NULL,
    start_offset integer NOT NULL CHECK (start_offset >= 0),
    end_offset integer NOT NULL CHECK (end_offset > start_offset),
    excerpt text NOT NULL,
    span_fingerprint text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    CHECK (expires_at > created_at),
    FOREIGN KEY (article_id, content_hash)
        REFERENCES rag_article_documents (article_id, content_hash)
)""",
    """CREATE INDEX rag_article_documents_fts_idx
    ON rag_article_documents USING gin (search_vector)""",
    """CREATE INDEX rag_chunks_embedding_hnsw_idx
    ON rag_chunks USING hnsw (embedding vector_cosine_ops)""",
    """CREATE INDEX rag_enrichment_jobs_claim_idx
    ON rag_enrichment_jobs (status, lease_expires_at, claim_generation)""",
    """CREATE INDEX rag_enrichment_jobs_eligible_idx
    ON rag_enrichment_jobs (status, next_attempt_at, created_at)""",
    """CREATE UNIQUE INDEX rag_generation_identity ON rag_index_generations (
    article_id, content_hash, normalization_version, chunker_version,
    embedding_model, embedding_version, embedding_dimension, corpus_generation
)""",
    """CREATE INDEX rag_source_references_expiry_idx
    ON rag_source_references (expires_at)""",
    """CREATE FUNCTION rag_immutable_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'article revisions are immutable';
END;
$$""",
    """CREATE TRIGGER rag_article_immutable BEFORE UPDATE OR DELETE
ON rag_article_documents FOR EACH ROW EXECUTE FUNCTION rag_immutable_revision()""",
    """CREATE TRIGGER rag_enrichment_publication_immutable BEFORE UPDATE OR DELETE
ON rag_enrichment_publications FOR EACH ROW
EXECUTE FUNCTION rag_immutable_revision()""",
    """CREATE VIEW rag_current_lexical AS
SELECT document.* FROM rag_article_documents document
JOIN rag_article_current current USING (article_id, content_hash)
WHERE current.tombstoned_at IS NULL""",
    """CREATE VIEW rag_current_vectors AS
SELECT chunk.* FROM rag_chunks chunk
JOIN rag_article_current current
ON current.article_id = chunk.article_id AND current.content_hash = chunk.content_hash
AND current.active_index_generation = chunk.generation
JOIN rag_index_generations generation ON generation.generation = chunk.generation
WHERE current.tombstoned_at IS NULL AND generation.status = 'ACTIVE'""",
    """CREATE TABLE rag_schema_migrations (
    filename text PRIMARY KEY, checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp())""",
)


def upgrade() -> None:
    """Create the post-007 schema and record the historical ledger."""
    connection = op.get_bind()
    for statement in BASELINE_DDL:
        _ = execute(connection, statement)
    for filename, checksum in LEGACY_CHECKSUMS:
        _ = execute(
            connection,
            "INSERT INTO rag_schema_migrations VALUES (%s, %s, DEFAULT)",
            (filename, checksum),
        )


def downgrade() -> None:
    """Refuse: dropping the baseline would delete every RAG row."""
    msg = "rag_0001_baseline cannot be downgraded; restore from backup instead"
    raise RuntimeError(msg)
