CREATE FUNCTION rag_immutable_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'article revisions are immutable';
END;
$$;
CREATE TRIGGER rag_article_immutable BEFORE UPDATE OR DELETE
ON rag_article_documents FOR EACH ROW EXECUTE FUNCTION rag_immutable_revision();

ALTER TABLE rag_article_current ADD CONSTRAINT rag_active_generation_fk
FOREIGN KEY (active_index_generation) REFERENCES rag_index_generations(generation);

ALTER TABLE rag_enrichment_jobs ADD CONSTRAINT rag_job_status_check
CHECK (status IN ('PENDING', 'PROCESSING', 'READY', 'FAILED', 'STALE'));

CREATE VIEW rag_current_lexical AS
SELECT document.* FROM rag_article_documents document
JOIN rag_article_current current USING (article_id, content_hash)
WHERE current.tombstoned_at IS NULL;

CREATE VIEW rag_current_vectors AS
SELECT chunk.* FROM rag_chunks chunk
JOIN rag_article_current current
ON current.article_id = chunk.article_id AND current.content_hash = chunk.content_hash
AND current.active_index_generation = chunk.generation
JOIN rag_index_generations generation ON generation.generation = chunk.generation
WHERE current.tombstoned_at IS NULL AND generation.status = 'ACTIVE';
