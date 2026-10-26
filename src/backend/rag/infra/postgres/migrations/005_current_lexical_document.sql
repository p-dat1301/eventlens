-- 003 expanded document.* before 004 added document_json; re-expand the view.
CREATE OR REPLACE VIEW rag_current_lexical AS
SELECT document.* FROM rag_article_documents document
JOIN rag_article_current current USING (article_id, content_hash)
WHERE current.tombstoned_at IS NULL;
