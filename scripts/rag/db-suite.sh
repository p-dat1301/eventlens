#!/bin/sh
# Real PostgreSQL suite; needs EVENTLENS_RAG_DATABASE_URL. Any skip fails the run.
set -u
if [ -n "${G1_EVIDENCE_DIR:-}" ]; then
    log="$G1_EVIDENCE_DIR/pytest-db.log"
else
    log=$(mktemp)
    trap 'rm -f "$log"' EXIT HUP INT TERM
fi
status=0
# tests/backend/rag/worker holds the T4 rag-worker CLI real-DB flows (ingest/enrich);
# tests/backend/rag/presentation holds the T7 HTTP path on PostgreSQL.
# Same scope as before the refactor: RAG, event pipeline, CRUD and architecture.
# Run once per schema path: fresh (Alembic from empty) and adopted
# (historical 001-007 runner, verified baseline stamp, Alembic upgrade).
: >"$log"
for path in fresh adopted; do
    echo "== schema path: $path" >>"$log"
    EVENTLENS_RAG_TEST_SCHEMA_PATH=$path uv run pytest tests/backend/rag \
        tests/backend/event_pipeline tests/backend/crud tests/architecture -q -rs \
        >>"$log" 2>&1 || status=$?
done
grep -E "^== schema path|passed|failed|error" "$log" | tail -n 15
if grep -q "skipped" "$log"; then
    echo "rag-db-test: skipped tests are not real database evidence" >&2
    status=1
fi
exit "$status"
