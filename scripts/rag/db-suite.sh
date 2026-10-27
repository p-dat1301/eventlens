#!/bin/sh
# Real PostgreSQL suite; needs EVENTLENS_RAG_DATABASE_URL. Any skip fails the run.
# Runs once per schema path, each in its own empty database on the same test
# server: fresh (Alembic from empty) and adopted (historical 001-007 runner,
# verified baseline stamp, Alembic upgrade). Separate databases keep the
# adopted path off rows and schema left by the fresh path.
# Each path writes pytest-db-<path>.log (full output) and pytest-db-<path>.xml
# (JUnit) into the evidence directory; failing test IDs and their tracebacks
# are printed so CI logs name what broke.
set -u
dir=${G1_EVIDENCE_DIR:-${G3_EVIDENCE_DIR:-}}
if [ -z "$dir" ]; then
    dir=$(mktemp -d)
fi
status=0
# tests/backend/rag/worker holds the T4 rag-worker CLI real-DB flows (ingest/enrich);
# tests/backend/rag/presentation holds the T7 HTTP path on PostgreSQL.
# Same scope as before the refactor: RAG, event pipeline, CRUD and architecture.
here=$(dirname "$0")
for path in fresh adopted; do
    log="$dir/pytest-db-$path.log"
    code=0
    if ! url=$(uv run python "$here/path_database.py" "$path" 2>"$log"); then
        echo "== schema path: $path: could not create its database" >&2
        cat "$log" >&2
        status=1
        continue
    fi
    EVENTLENS_RAG_DATABASE_URL=$url EVENTLENS_RAG_TEST_SCHEMA_PATH=$path \
        uv run pytest tests/backend/rag \
            tests/backend/event_pipeline tests/backend/crud tests/architecture \
            -q -rfEs --junitxml="$dir/pytest-db-$path.xml" >"$log" 2>&1 || code=$?
    echo "== schema path: $path (exit $code)"
    tail -n 1 "$log"
    if [ "$code" -ne 0 ]; then
        status=$code
        grep -E "^(FAILED|ERROR) " "$log" || true
        # First 200 lines of the FAILURES/ERRORS section (tracebacks).
        awk '/^=+ (FAILURES|ERRORS) =+$/ { show = 1 } show' "$log" | head -n 200
    fi
    if grep -q "skipped" "$log"; then
        echo "rag-db-test: skipped tests in '$path' are not real database evidence" >&2
        status=1
    fi
done
echo "rag-db-test: full logs and JUnit XML in $dir"
exit "$status"
