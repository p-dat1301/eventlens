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
# apps/worker/tests holds the T4 worker CLI real-DB flows (ingest/enrich).
uv run --package eventlens-rag pytest apps/rag/tests apps/worker/tests -q -rs \
    >"$log" 2>&1 || status=$?
tail -n 15 "$log"
if grep -q "skipped" "$log"; then
    echo "rag-db-test: skipped tests are not real database evidence" >&2
    status=1
fi
exit "$status"
