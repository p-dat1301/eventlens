-- T7 source viewer (D37): opaque, caller-bound citation references shared by
-- every API process. Only the SHA-256 of the random token is stored. A row
-- names one exact article revision and span; the viewer still re-checks the
-- current revision, tombstone and span on every read. Applied SQL 001-006
-- stays unchanged.
CREATE TABLE rag_source_references (
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
);

CREATE INDEX rag_source_references_expiry_idx
    ON rag_source_references (expires_at);
