ALTER TABLE rag_article_documents ADD COLUMN document_json jsonb;
ALTER TABLE rag_index_generations
DROP CONSTRAINT rag_initial_embedding_identity;
ALTER TABLE rag_index_generations ADD COLUMN article_id uuid;
ALTER TABLE rag_index_generations ADD COLUMN content_hash text;
ALTER TABLE rag_index_generations ADD COLUMN normalization_version text NOT NULL DEFAULT 'nfc.v1';
ALTER TABLE rag_index_generations ADD COLUMN corpus_generation text NOT NULL DEFAULT 'rag-fixture-corpus.v1';
ALTER TABLE rag_index_generations ADD COLUMN expected_chunks integer NOT NULL DEFAULT 0;
ALTER TABLE rag_index_generations ADD CONSTRAINT rag_generation_revision_fk
FOREIGN KEY (article_id, content_hash) REFERENCES rag_article_documents(article_id, content_hash);
CREATE UNIQUE INDEX rag_generation_identity ON rag_index_generations (
    article_id, content_hash, normalization_version, chunker_version,
    embedding_model, embedding_version, embedding_dimension, corpus_generation
);
CREATE TABLE rag_enrichment_versions (
    job_id uuid PRIMARY KEY REFERENCES rag_enrichment_jobs(job_id),
    generation bigint NOT NULL REFERENCES rag_index_generations(generation),
    metadata jsonb NOT NULL,
    published_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
