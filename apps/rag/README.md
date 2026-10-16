# EventLens RAG

Reusable T3 foundation. Domain, application, and ports are framework-free.
LangGraph resides only in `graph`; PostgreSQL and fake adapters reside in `adapters`.
All embeddings are deterministic local fakes with 1536 dimensions.

## Local database commands

All commands read `EVENTLENS_RAG_DATABASE_URL` and exit `3` with
`rag_storage_unavailable` when PostgreSQL cannot be reached.

- `eventlens-rag migrate` applies checksum-verified migrations in one transaction;
  any failure rolls back schema changes and ledger rows together. Never edit an
  applied migration: add a new numbered file instead.
- Migration SQL source of truth is `src/eventlens_rag/migrations/` (shipped in the
  wheel and read at runtime). `migrations/` is a byte-identical checked-in copy;
  `test_bundled_sql_preserves_original_migration_bytes` fails if the file sets or
  bytes diverge. Missing SQL fails before opening a database or reporting success.
- Untracked legacy tables (created before the migration ledger, e.g. `rag_chunks`
  without `generation`) stop `migrate` with `rag_migration_failed` and change
  nothing. Existing pre-004 rows cannot be reconstructed from title/content alone:
  migration stops until exact original ArticleDocuments are restored from an
  approved source; never fabricate missing document fields.
- `eventlens-rag load-fixtures` persists `fixtures/rag/articles`; lexical FTS is
  available immediately, before any enrichment.
- `eventlens-rag rebuild` stages fake-embedding generations for current articles
  (reusing complete matching generations) and activates them in one transaction.

## Local Compose database

The Compose `eventlens` database may hold untracked pre-ledger RAG tables, so the
Make targets use a separate `eventlens_rag` database on the same server
(override with `RAG_DATABASE_NAME` or `RAG_DATABASE_URL`):

```sh
make infra
make rag-db-create      # idempotent CREATE DATABASE eventlens_rag
make rag-migrate
make rag-load-fixtures
make rag-rebuild        # second run reuses every generation
```

Legacy recovery: leave the `eventlens` database untouched. Its RAG tables have no
ledger and cannot be upgraded in place. To carry legacy content forward, re-ingest
the exact original ArticleDocuments from their approved source into
`eventlens_rag` with the current CLI; dropping or archiving the legacy tables
requires the owner's explicit approval.

## Test harness

- `make rag-db-test` runs the full suite against an isolated, auto-removed
  `pgvector/pgvector:0.8.0-pg17` container; any skipped test fails the run.
- `make gate-g1` runs the G1 technical checks (worker regression, migrate,
  load-fixtures, rebuild twice with generation reuse, DB suite, unavailable-DB
  exit 3) against an isolated database. Exit 1 means a technical failure; exit 2
  means technical PASS with G1 acceptance still OPEN. The script never records
  G1 acceptance: that needs F1-F4 review verdicts, D41 approval evidence (or the
  owner's explicit acceptance of its absence) and explicit user acceptance. The
  presence of `apps/worker` alone establishes nothing. G1 was accepted on
  2026-10-16, scoped to T1-T3 fixture/fake with the separate `eventlens_rag`
  database; see `.omo/evidence/G1/acceptance-decision-2026-10-16.md`. Exit 2 from
  the script still only means technical PASS.
