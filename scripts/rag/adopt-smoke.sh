#!/bin/sh
# Adoption smoke on an isolated DB: historical 001-007 runner -> data -> the
# Alembic CLI refuses it -> preflight -> adopt-legacy -> migrate -> rebuild ->
# worker ingest/enrich -> loopback rag-api briefing + citation. Needs
# EVENTLENS_RAG_DATABASE_URL (use via with-test-db.sh; never a real DB).
set -eu
out=${ADOPT_EVIDENCE_DIR:-$(mktemp -d)}
api_pid=
cleanup() {
    if [ -n "$api_pid" ]; then
        kill "$api_pid" 2>/dev/null || true
        wait "$api_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT HUP INT TERM
# step LABEL EXPECTED_EXIT CMD...: record exit code; stop if it differs.
step() {
    label=$1
    want=$2
    shift 2
    code=0
    "$@" >"$out/$label.log" 2>&1 || code=$?
    tail -n 8 "$out/$label.log"
    printf '%s\t%s\t%s\t%s\n' "$label" "$code" "$want" "$*" >>"$out/exit-codes.tsv"
    [ "$code" -eq "$want" ] || exit 1
}
step legacy-runner 0 uv run python -c \
    "import os; from backend.rag.infra.postgres.migrations import migrate; migrate(os.environ['EVENTLENS_RAG_DATABASE_URL'])"
step legacy-load-fixtures 0 uv run eventlens-rag load-fixtures
step migrate-refuses-unadopted 1 uv run eventlens-rag migrate
step worker-refuses-unadopted 1 uv run eventlens-rag-worker enrich --max-jobs 1
step preflight 0 uv run eventlens-rag preflight
step adopt-legacy 0 uv run eventlens-rag adopt-legacy
step adopt-again 0 uv run eventlens-rag adopt-legacy
step migrate 0 uv run eventlens-rag migrate
step rebuild 0 uv run eventlens-rag rebuild
step worker-ingest 0 uv run eventlens-rag-worker ingest fixtures/article_bundle.v1.json
step worker-enrich 0 uv run eventlens-rag-worker enrich --max-jobs 5
port=$(uv run python -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
EVENTLENS_API_RAG_DATABASE_URL=$EVENTLENS_RAG_DATABASE_URL \
    uv run uvicorn backend.rag.api_main:app --host 127.0.0.1 --port "$port" \
    >"$out/rag-api.log" 2>&1 &
api_pid=$!
step api-health 0 uv run python "$(dirname "$0")/api_smoke.py" health "http://127.0.0.1:$port"
step api-briefing-citation 0 uv run python "$(dirname "$0")/api_smoke.py" briefing "http://127.0.0.1:$port"
echo "adopt-smoke: all steps held ($out/exit-codes.tsv)"
