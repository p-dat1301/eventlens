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
uv run pytest tests/backend/rag tests/backend/event_pipeline tests/backend/crud \
    tests/architecture -q -rs \
    >"$log" 2>&1 || status=$?
tail -n 15 "$log"
if grep -q "skipped" "$log"; then
    echo "rag-db-test: skipped tests are not real database evidence" >&2
    status=1
fi
exit "$status"
