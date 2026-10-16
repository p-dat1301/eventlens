-- T4 enrichment lifecycle: attempt budget, retry schedule, fenced cancellation
-- and an append-only publication history. Applied SQL 001-005 stays unchanged.
ALTER TABLE rag_enrichment_jobs
    ADD COLUMN attempt_count integer NOT NULL DEFAULT 0
        CHECK (attempt_count >= 0),
    ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ADD COLUMN failure_code text,
    ADD COLUMN retry_policy_id text,
    ADD COLUMN cancel_requested_generation bigint,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT clock_timestamp();

CREATE INDEX rag_enrichment_jobs_eligible_idx
    ON rag_enrichment_jobs (status, next_attempt_at, created_at);

-- Every fenced publication appends one immutable row; nothing here is deleted
-- when a job is re-enqueued, so history survives STALE -> PENDING requeues.
CREATE TABLE rag_enrichment_publications (
    publication_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES rag_enrichment_jobs(job_id),
    claim_generation bigint NOT NULL,
    index_generation bigint NOT NULL REFERENCES rag_index_generations(generation),
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
);

CREATE TRIGGER rag_enrichment_publication_immutable BEFORE UPDATE OR DELETE
ON rag_enrichment_publications FOR EACH ROW EXECUTE FUNCTION rag_immutable_revision();
