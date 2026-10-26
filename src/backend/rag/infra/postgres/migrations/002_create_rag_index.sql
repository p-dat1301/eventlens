CREATE TABLE IF NOT EXISTS rag_article_documents (
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
    PRIMARY KEY (article_id, content_hash)
);

CREATE TABLE IF NOT EXISTS rag_article_current (
    article_id uuid PRIMARY KEY,
    content_hash text NOT NULL,
    active_index_generation bigint,
    tombstoned_at timestamptz,
    FOREIGN KEY (article_id, content_hash)
        REFERENCES rag_article_documents (article_id, content_hash)
);

CREATE TABLE IF NOT EXISTS rag_enrichment_jobs (
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
    UNIQUE (article_id, content_hash, enrichment_version, model_version, prompt_version),
    FOREIGN KEY (article_id, content_hash)
        REFERENCES rag_article_documents (article_id, content_hash)
);

CREATE TABLE IF NOT EXISTS rag_index_generations (
    generation bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    embedding_model text NOT NULL,
    embedding_version text NOT NULL,
    embedding_dimension integer NOT NULL CHECK (embedding_dimension = 1536),
    chunker_version text NOT NULL,
    status text NOT NULL DEFAULT 'STAGING',
    indexed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT rag_initial_embedding_identity UNIQUE (
        embedding_model, embedding_version, embedding_dimension, chunker_version)
);

CREATE TABLE IF NOT EXISTS rag_chunks (
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
);

CREATE INDEX IF NOT EXISTS rag_article_documents_fts_idx
    ON rag_article_documents USING gin (search_vector);
CREATE INDEX IF NOT EXISTS rag_chunks_embedding_hnsw_idx
    ON rag_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS rag_enrichment_jobs_claim_idx
    ON rag_enrichment_jobs (status, lease_expires_at, claim_generation);
